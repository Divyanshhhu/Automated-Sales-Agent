"""Bounded evidence retrieval via Tavily.

This is a fixed loop over a fixed signal taxonomy -- NOT an agent deciding
how much to search. That keeps cost and latency predictable per company
and is what lets the memo generator's grounding be checkable afterward.
"""
import re
from datetime import datetime, timezone

import requests

from .db import get_connection
from .tavily_client import tavily_search

RESULTS_PER_CATEGORY = 2

# direct_pain_point is the one category that can make a signal "Strong" (see
# confidence.py). Complaints about a company's responsiveness live on
# consumer review/complaint sites, not the general web -- a general-web
# query returned nothing usable for any company in live runs -- so this
# category searches those sites. They're noisy (other companies, site
# boilerplate), so more results are fetched and each must pass the checks
# below. Same Tavily cost: one credit per search regardless of result count.
PAIN_POINT_CATEGORY = "direct_pain_point"
PAIN_POINT_SITES = ["mouthshut.com", "consumercomplaints.in", "voxya.com", "complaintboard.in", "reddit.com"]
PAIN_POINT_RESULTS = 5

# A pain point must be about *responsiveness* -- the problem the product
# solves -- not any complaint. Live examples of what must NOT count:
# "government approval-related delays" (unrelated delay), "customer
# collections" (positive), and complaint-site boilerplate like "File a
# complaint and get it resolved by ... customer care". So a clause needs a
# negative term AND a communication term; bare "complaint"/"delay" isn't one.
NEGATIVE_TERMS = (
    "slow", "unresponsive", "no response", "not responding", "not respond", "no reply", "never replied",
    "no update", "no one", "nobody", "ignored", "ignoring", "unreachable", "not reachable",
    "no follow", "never called", "not called", "waiting", "delay", "delayed", "frustrat",
    "dissatisf", "unhappy", "pathetic", "worst", "poor",
)
COMMUNICATION_TERMS = (
    "response", "respond", "reply", "replied", "revert", "call back", "callback", "called", "calls",
    "follow up", "follow-up", "followup", "update", "customer care", "customer service",
    "helpline", "contact", "reach", "enquir", "inquir", "query", "queries", "email", "mail",
)


def _is_genuine_pain_point(text: str) -> bool:
    # Checked per clause, not per snippet: a live 800-char snippet held both
    # "customer collections" and "government approval-related delays",
    # which satisfied a whole-snippet check without either being about
    # response time.
    for clause in re.split(r"[.!?•\n]", text):
        lowered = clause.lower()
        if any(t in lowered for t in NEGATIVE_TERMS) and any(t in lowered for t in COMMUNICATION_TERMS):
            return True
    return False


_GENERIC_NAME_WORDS = frozenset({"the", "limited", "ltd", "pvt", "private", "llp", "company", "co", "inc"})


def mentions_company(result: dict, company_name: str) -> bool:
    """Whether a search result is about this company: its first two
    distinctive name words appear in the title, text or URL. Review sites
    return other companies' pages for the same query (live: Marathon Realty
    and Kalpataru for an Adani Realty / Rustomjee search).
    """
    words = [w for w in re.findall(r"[a-z0-9]+", company_name.lower()) if w not in _GENERIC_NAME_WORDS]
    if not words:
        return False
    key = " ".join(words[:2])
    haystack = " ".join(
        str(result.get(field) or "") for field in ("title", "content", "url")
    ).lower()
    return key in " ".join(re.findall(r"[a-z0-9]+", haystack))


def retrieve_evidence_for_company(
    profile_id: int, company_id: str, company_name: str, signal_taxonomy: dict
) -> int:
    """Run one bounded search per signal category for this company.

    Evidence is stored per profile, because each profile has its own signal
    queries -- evidence fetched for one profile's query never stands in for
    another's.

    Idempotent: categories that already have stored evidence (from an earlier,
    partially failed run) are skipped rather than re-queried, and the unique
    index on evidence_items drops any duplicate row that slips through.

    Returns the number of new evidence items stored.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    already_covered = {
        row["category"]
        for row in conn.execute(
            "SELECT DISTINCT category FROM evidence_items WHERE profile_id=? AND company_id=?",
            (profile_id, company_id),
        )
    }
    stored = 0
    for category, query_template in signal_taxonomy.items():
        if category in already_covered:
            continue
        query = query_template.format(company=company_name)
        is_pain_point = category == PAIN_POINT_CATEGORY
        try:
            if is_pain_point:
                # no time_range: review pages accumulate complaints over years;
                # each item keeps its source date for the reviewer to judge
                results = tavily_search(
                    query,
                    max_results=PAIN_POINT_RESULTS,
                    include_domains=PAIN_POINT_SITES,
                    include_answer=False,
                )
            else:
                results = tavily_search(
                    query, max_results=RESULTS_PER_CATEGORY, time_range="year", include_answer=False
                )
        except requests.exceptions.RequestException:
            continue
        for r in results:
            content = r.get("content", "")
            if is_pain_point and not (mentions_company(r, company_name) and _is_genuine_pain_point(content)):
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO evidence_items
                    (profile_id, company_id, category, fact_text, source_url, source_date, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile_id,
                    company_id,
                    category,
                    content[:800],
                    r.get("url"),
                    r.get("published_date"),
                    now,
                ),
            )
            stored += cursor.rowcount
        conn.commit()  # per category, so a crash mid-company keeps finished categories
    conn.close()
    return stored


def get_evidence_for_company(profile_id: int, company_id: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM evidence_items WHERE profile_id=? AND company_id=? ORDER BY id",
        (profile_id, company_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
