"""Deterministic company discovery: two API calls, no LLM.

Apollo's paid-only Organization Search would normally do this in one call,
but that endpoint requires a paid plan. Instead: Exa's neural/entity search
finds candidate company domains for the ICP's geography/industry (Exa
benchmarks meaningfully better than Tavily on company-discovery-style
queries -- see exa_client.py), then Apollo's free-tier Organization
Enrichment endpoint (single domain lookup) pulls structured firmographic
data per candidate. Still a fixed, bounded set of queries -- not an
open-ended agentic search loop.
"""
import json
import os
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests

from .db import get_connection
from .exa_client import exa_search
from .retry import call_with_retry, is_retriable_requests_error

APOLLO_ENRICH_URL = "https://api.apollo.io/api/v1/organizations/enrich"

# Real estate portals, aggregators, media, and generic platforms that show up
# in "top real estate developer" searches but are not themselves ICP targets.
# Every domain below burned an Apollo enrichment credit on a company that was
# never going to qualify -- confirmed junk from live testing, not a guess.
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

# Content-based signals that a search result is a job board, media outlet, or
# SaaS product rather than an actual real estate company -- catches junk that
# isn't on the domain blocklist yet. Checked against the search result's
# title + snippet before an Apollo credit is spent on it.
NON_COMPANY_CONTENT_MARKERS = (
    "hiring alert", "walk-in interview", "job vacancy", "recruitment",
    "career opportunities", "magazine", "business leaders edition",
    "crm software", "software guide", "erp software", "data intelligence platform",
    "market research platform",
)


def _domain_from_url(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lower()
    except ValueError:
        return None
    return netloc[4:] if netloc.startswith("www.") else netloc or None


def _looks_like_non_company(result: dict) -> bool:
    text = f"{result.get('title', '')} {result.get('content', '')}".lower()
    return any(marker in text for marker in NON_COMPANY_CONTENT_MARKERS)


# Fallback for arbitrary off-topic noise a negative blocklist can't anticipate
# -- one live test returned dictionary sites and entertainment companies
# (billboard.com, topgolf.com) for a real-estate query. A blocklist only
# catches junk we've already seen; this positive check requires the result
# to actually be about the industry at all.
_RELEVANCE_FALLBACK_TERMS = (
    "real estate", "realty", "developer", "property", "properties",
    "residential", "apartment", "villa", "township", "construction", "housing",
)


def _looks_relevant(result: dict, icp: dict) -> bool:
    text = f"{result.get('title', '')} {result.get('content', '')}".lower()
    keywords = [kw.lower() for kw in icp["industry_keywords"]] + list(_RELEVANCE_FALLBACK_TERMS)
    return any(kw in text for kw in keywords)


def find_candidate_domains(icp: dict, max_candidates: int = 25) -> list[str]:
    """Fixed, bounded set of searches -- one per geography -- not open-ended.

    Three filters run before a domain ever reaches Apollo (which is where
    the real cost is): the known-junk blocklist is passed to Exa directly so
    it's excluded at the source, a content-based check catches job
    boards/media/SaaS sites that aren't blocklisted by domain yet, and a
    positive relevance check catches arbitrary off-topic noise a blocklist
    can't anticipate.
    """
    domains: list[str] = []
    seen = set()
    for geography in icp["geographies"]:
        if len(domains) >= max_candidates:
            break
        for keyword in icp["industry_keywords"][:1]:  # one keyword per geography keeps this bounded
            query = f"top {keyword} companies in {geography}"
            try:
                results = exa_search(
                    query, num_results=8, category="company", exclude_domains=list(DOMAIN_BLOCKLIST)
                )
            except requests.exceptions.RequestException:
                continue
            for r in results:
                domain = _domain_from_url(r.get("url", ""))
                if not domain or domain in seen:
                    continue
                if any(blocked in domain for blocked in DOMAIN_BLOCKLIST):
                    continue
                if _looks_like_non_company(r):
                    continue
                if not _looks_relevant(r, icp):
                    continue
                seen.add(domain)
                domains.append(domain)
                if len(domains) >= max_candidates:
                    break
    return domains


def enrich_domain(domain: str) -> dict | None:
    api_key = os.environ.get("APOLLO_API_KEY")
    if not api_key:
        raise RuntimeError("APOLLO_API_KEY is not set in the environment (.env)")

    def _do_request():
        resp = requests.get(
            APOLLO_ENRICH_URL,
            headers={"x-api-key": api_key},
            params={"domain": domain},
            timeout=30,
        )
        if resp.status_code == 404:
            return resp  # not found is a normal outcome, not a failure -- don't retry it
        resp.raise_for_status()
        return resp

    resp = call_with_retry(
        _do_request, is_retriable=is_retriable_requests_error, context=f"Apollo enrich({domain})"
    )
    if resp.status_code == 404:
        return None
    return resp.json().get("organization")


def discover_companies(icp: dict, per_page: int = 25, page: int = 1) -> list[dict]:
    """Returns a list of enriched organization dicts, same shape downstream
    code expects regardless of which Apollo endpoint produced them.
    """
    domains = find_candidate_domains(icp, max_candidates=per_page)
    organizations = []
    for domain in domains:
        try:
            org = enrich_domain(domain)
        except requests.exceptions.RequestException:
            continue  # one domain's enrichment failing shouldn't abort the whole batch
        if org:
            organizations.append(org)
    return organizations


def store_companies(raw_companies: list[dict]) -> list[str]:
    """Upsert discovered companies into the DB. Returns list of company_ids stored."""
    conn = get_connection()
    ids = []
    now = datetime.now(timezone.utc).isoformat()
    for org in raw_companies:
        company_id = org.get("id") or org.get("primary_domain") or org.get("name")
        if not company_id:
            continue
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
                company_id,
                org.get("name"),
                org.get("primary_domain") or org.get("website_url"),
                org.get("estimated_num_employees"),
                org.get("industry"),
                org.get("city"),
                org.get("state"),
                org.get("country"),
                org.get("short_description"),
                json.dumps(org),
                now,
            ),
        )
        ids.append(company_id)
    conn.commit()
    conn.close()
    return ids
