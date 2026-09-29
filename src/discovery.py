"""Deterministic company discovery: one Exa search per geography, no LLM.

Exa's company search (category="company") returns structured firmographics
-- employee count, headquarters, description -- alongside each result, so a
single call both finds candidates and enriches them. This replaced a
separate Apollo enrichment call per domain, whose free-tier credits were the
tightest budget in the whole pipeline. Still a fixed, bounded set of queries
-- not an open-ended agentic search loop.
"""
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TypedDict
from urllib.parse import urlparse

import requests

from .db import get_connection
from .exa_client import exa_search

logger = logging.getLogger("sales_agent")

# 25 results per search cost ~$0.022 vs ~$0.007 for 8 (live-checked
# 2026-09-29) and triple the pool of companies each geography can supply --
# with only one search per geography, that pool is what limits new finds.
RESULTS_PER_SEARCH = 25

# Real estate portals, aggregators, media, and generic platforms that show up
# in "top real estate developer" searches but are not themselves ICP targets
# -- confirmed junk from live testing, not a guess.
DOMAIN_BLOCKLIST = {
    "wikipedia.org", "linkedin.com", "facebook.com", "twitter.com", "x.com",
    "youtube.com", "instagram.com", "magicbricks.com", "99acres.com",
    "housing.com", "nobroker.in", "squareyards.com", "makaan.com",
    "commonfloor.com", "indiamart.com", "justdial.com", "naukri.com", "business-standard.com",
    "economictimes.indiatimes.com", "moneycontrol.com", "livemint.com",
    "timesofindia.indiatimes.com", "ndtv.com", "reuters.com", "bloomberg.com",
    "google.com", "bing.com",
    # confirmed junk from live batches: finance/data sites, SaaS tools, media,
    # job boards -- none of these are real estate companies
    "yahoo.com", "scribd.com", "rocketreach.co", "revenuebase.ai",
    "bullfincher.io", "telecrm.in", "theceo.in", "tracxn.com",
    "constructionplacements.com",
    # generic platforms that surface in "top X companies" searches for any
    # industry -- confirmed junk from testing the guard itself
    "reddit.com", "quora.com", "glassdoor.com", "glassdoor.co.in",
    "slideshare.net", "f6s.com", "clutch.co",
}

# Exa sometimes links a result to its own company profile page
# (exa.ai/library/organization/...) instead of the company's website. The
# entity data is still valid, but the domain is Exa's, not the company's.
EXA_PROFILE_DOMAIN = "exa.ai"

# Content-based signals that a search result is a job board, media outlet, or
# SaaS product rather than an actual real estate company -- catches junk that
# isn't on the domain blocklist yet.
NON_COMPANY_CONTENT_MARKERS = (
    "hiring alert", "walk-in interview", "job vacancy", "recruitment",
    "career opportunities", "magazine", "business leaders edition",
    "crm software", "software guide", "erp software", "data intelligence platform",
    "market research platform",
)

# Fallback for arbitrary off-topic noise a negative blocklist can't anticipate
# -- one live test returned dictionary sites and entertainment companies
# (billboard.com, topgolf.com) for a real-estate query. A blocklist only
# catches junk we've already seen; this positive check requires the result
# to actually be about the industry at all.
_RELEVANCE_FALLBACK_TERMS = (
    "real estate", "realty", "developer", "property", "properties",
    "residential", "apartment", "villa", "township", "construction", "housing",
)


class Company(TypedDict):
    """Normalized company record -- the shape store_companies() persists."""

    id: str
    name: str
    domain: str | None
    employee_count: int | None
    industry: str | None
    city: str | None
    state: str | None
    country: str | None
    short_description: str | None
    raw: dict


def _domain_from_url(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return None
    return netloc[4:] if netloc.startswith("www.") else netloc or None


def _is_blocked(domain: str) -> bool:
    # Exact domain or a subdomain of it -- a bare substring check blocked
    # tatahousing.com (a real ICP target) for containing "housing.com".
    return any(domain == blocked or domain.endswith("." + blocked) for blocked in DOMAIN_BLOCKLIST)


def _result_text(result: dict) -> str:
    entity = result.get("entity") or {}
    description = entity.get("description") if isinstance(entity.get("description"), str) else ""
    return f"{result.get('title', '')} {result.get('content', '')} {description}".lower()


def _looks_like_non_company(result: dict) -> bool:
    text = _result_text(result)
    return any(marker in text for marker in NON_COMPANY_CONTENT_MARKERS)


def _looks_relevant(result: dict, icp: dict) -> bool:
    text = _result_text(result)
    keywords = [kw.lower() for kw in icp["industry_keywords"]] + list(_RELEVANCE_FALLBACK_TERMS)
    return any(kw in text for kw in keywords)


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_str(value: object) -> str | None:
    return (value.strip() or None) if isinstance(value, str) else None


def company_from_result(result: dict) -> Company | None:
    """Maps an Exa result's company entity to a Company, or None if the
    result carries no usable company entity (i.e. it isn't a company profile).
    """
    entity = result.get("entity")
    if not entity:
        return None
    name = _as_str(entity.get("name"))
    if not name:
        return None

    domain = _domain_from_url(result.get("url", ""))
    if domain == EXA_PROFILE_DOMAIN:
        domain = None
    workforce = entity.get("workforce")
    hq = entity.get("headquarters")
    hq = hq if isinstance(hq, dict) else {}

    return Company(
        # Domain is the stable identity across reruns; name is the fallback
        # only when Exa didn't give us the company's own website.
        id=domain or f"name:{name.lower()}",
        name=name,
        domain=domain,
        employee_count=_as_int(workforce.get("total")) if isinstance(workforce, dict) else None,
        industry=None,  # Exa returns no industry label; the ICP scorer falls back to the description
        city=_as_str(hq.get("city")),
        state=None,  # not provided by Exa
        country=_as_str(hq.get("country")),
        short_description=_as_str(entity.get("description")),
        raw=entity,
    )


@dataclass(frozen=True)
class Discovery:
    companies: list[Company]  # new companies, up to the limit
    already_known: int  # valid results skipped because the profile has seen them before
    searched_locations: list[str]  # geographies actually searched (successfully) this time


def discover_companies(icp: dict, limit: int = 25, *, known_ids: frozenset[str] = frozenset()) -> Discovery:
    """Fixed, bounded set of searches -- one per geography -- not open-ended.

    `limit` counts *new* companies: results in `known_ids` (companies this
    profile has already evaluated) are skipped and the next geography is
    searched instead. Otherwise the same query returns the same top results
    and every rerun would rediscover the same handful of companies.

    Four filters run on every result: the known-junk domain blocklist
    (applied here, not sent to Exa -- see exa_client.py), results without a
    company entity are dropped, a content-based check catches job
    boards/media/SaaS sites that aren't blocklisted by domain yet, and a
    positive relevance check catches arbitrary off-topic noise a blocklist
    can't anticipate.
    """
    companies: list[Company] = []
    seen: set[str] = set()
    already_known = 0
    searched: list[str] = []
    for geography in icp["geographies"]:
        if len(companies) >= limit:
            break
        keyword = icp["industry_keywords"][0]  # one keyword per geography keeps this bounded
        query = f"top {keyword} companies in {geography}"
        try:
            results = exa_search(query, num_results=RESULTS_PER_SEARCH, category="company")
        except requests.exceptions.RequestException:
            continue  # one geography's search failing shouldn't abort the whole batch
        searched.append(geography)
        found_before, known_before = len(companies), already_known
        for r in results:
            domain = _domain_from_url(r.get("url", ""))
            if domain and _is_blocked(domain):
                continue
            if _looks_like_non_company(r) or not _looks_relevant(r, icp):
                continue
            company = company_from_result(r)
            if company is None or company["id"] in seen:
                continue
            seen.add(company["id"])
            if company["id"] in known_ids:
                already_known += 1
                continue
            companies.append(company)
            if len(companies) >= limit:
                break
        logger.info(
            "Discovery %r: %d results, %d with company data, %d already known, %d new companies kept",
            geography, len(results), sum(1 for r in results if r.get("entity")),
            already_known - known_before, len(companies) - found_before,
        )
    return Discovery(companies=companies, already_known=already_known, searched_locations=searched)


def store_companies(companies: list[Company]) -> list[str]:
    """Upsert discovered companies into the DB. Returns list of company_ids stored."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    for c in companies:
        conn.execute(
            """
            INSERT INTO companies (
                id, name, domain, employee_count, industry, city, state, country,
                short_description, raw_json, discovered_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name=excluded.name, domain=excluded.domain,
                employee_count=excluded.employee_count, industry=excluded.industry,
                city=excluded.city, state=excluded.state, country=excluded.country,
                short_description=excluded.short_description, raw_json=excluded.raw_json
            """,
            (
                c["id"], c["name"], c["domain"], c["employee_count"], c["industry"],
                c["city"], c["state"], c["country"], c["short_description"],
                json.dumps(c["raw"]), now,
            ),
        )
    conn.commit()
    conn.close()
    return [c["id"] for c in companies]
