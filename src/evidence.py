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

# direct_pain_point is the one category that gates the "Strong signal" label
# (see confidence.py), so an OR-heavy query returning a loosely-related page
# must not be allowed to count as a genuine complaint/delay signal just
# because it was fetched under that query slot. A bare substring match on a
# word like "delay" is not enough -- "government approval-related delays" in
# a results announcement matched on that basis alone during live testing and
# was not actually about customer response time. Require a negative-quality
# term to co-occur with a customer/response-context term.
NEGATIVE_QUALITY_TERMS = (
    "complaint", "complaints", "slow", "unresponsive", "negative review",
    "poor experience", "no response", "ignored", "waiting", "dissatisf",
    "frustrat", "unhappy", "delay", "delayed",
)
CUSTOMER_CONTEXT_TERMS = (
    "customer", "customers", "buyer", "buyers", "client", "clients",
    "lead", "leads", "inquiry", "inquiries", "enquiry", "enquiries",
    "response", "follow-up", "follow up", "service", "support", "review",
)


def _is_genuine_pain_point(text: str) -> bool:
    # Whole-blob co-occurrence isn't enough: a live test found "customer
    # collections" (financial, positive) and "government approval-related
    # delays" (unrelated) both present in the same 800-char snippet, which
    # satisfied a blob-level check without either phrase being about
    # customer response time. Require both terms in the SAME clause.
    clauses = re.split(r"[.!?•\n]", text)
    for clause in clauses:
        lowered = clause.lower()
        has_negative = any(kw in lowered for kw in NEGATIVE_QUALITY_TERMS)
        has_customer_context = any(kw in lowered for kw in CUSTOMER_CONTEXT_TERMS)
        if has_negative and has_customer_context:
            return True
    return False


def retrieve_evidence_for_company(company_id: str, company_name: str, signal_taxonomy: dict) -> int:
    """Run one bounded search per signal category for this company.

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
            "SELECT DISTINCT category FROM evidence_items WHERE company_id=?", (company_id,)
        )
    }
    stored = 0
    for category, query_template in signal_taxonomy.items():
        if category in already_covered:
            continue
        query = query_template.format(company=company_name)
        try:
            results = tavily_search(
                query, max_results=RESULTS_PER_CATEGORY, time_range="year", include_answer=False
            )
        except requests.exceptions.RequestException:
            continue
        for r in results:
            content = r.get("content", "")
            if category == "direct_pain_point" and not _is_genuine_pain_point(content):
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO evidence_items
                    (company_id, category, fact_text, source_url, source_date, retrieved_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
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


def get_evidence_for_company(company_id: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM evidence_items WHERE company_id=? ORDER BY id", (company_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
