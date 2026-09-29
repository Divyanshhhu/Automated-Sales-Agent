"""Deterministic ICP Fit scoring.

This is a plain weighted rule function over structured company attributes --
no LLM. ICP Fit is a separate axis from Signal Confidence (see confidence.py);
they must never be collapsed into one number.
"""
import json
from dataclasses import dataclass

from .db import get_connection, utc_now

WEIGHTS = {"employee": 30, "location": 40, "industry": 30}


def _employee_score(employee_count, min_emp, max_emp) -> float:
    if employee_count is None:
        return 0.5  # unknown -- partial credit, not a hard fail
    if min_emp <= employee_count <= max_emp:
        return 1.0
    # partial credit for being close (within 50% of the nearest bound)
    span = max_emp - min_emp
    distance = min_emp - employee_count if employee_count < min_emp else employee_count - max_emp
    if span > 0 and distance <= span * 0.5:
        return 0.5
    return 0.0


def _location_score(city, state, country, geographies: list[str]) -> float:
    fields = [f.lower() for f in (city, state, country) if f]
    geos = [g.lower() for g in geographies]
    for f in fields:
        for g in geos:
            if g in f or f in g:
                return 1.0
    return 0.0


def _industry_score(industry, short_description, keywords: list[str]) -> float:
    haystack = " ".join([t for t in (industry, short_description) if t]).lower()
    if not haystack:
        return 0.3  # unknown -- small partial credit
    for kw in keywords:
        if kw.lower() in haystack:
            return 1.0
    return 0.0


@dataclass(frozen=True)
class IcpScore:
    score: float
    label: str
    # Points earned per criterion, e.g. {"employee": 30.0, "location": 40.0,
    # "industry": 0.0} -- lets a reviewer see *why* a company scored what it did.
    breakdown: dict[str, float]


def score_company(company_row, icp: dict) -> IcpScore:
    min_emp, max_emp = icp["employee_range"]
    fractions = {
        "employee": _employee_score(company_row["employee_count"], min_emp, max_emp),
        "location": _location_score(
            company_row["city"], company_row["state"], company_row["country"], icp["geographies"]
        ),
        "industry": _industry_score(
            company_row["industry"], company_row["short_description"], icp["industry_keywords"]
        ),
    }
    breakdown = {k: round(fractions[k] * WEIGHTS[k], 1) for k in WEIGHTS}
    score = round(sum(breakdown.values()), 1)
    if score >= 70:
        label = "High"
    elif score >= 40:
        label = "Medium"
    else:
        label = "Low"
    return IcpScore(score=score, label=label, breakdown=breakdown)


def score_companies_for_profile(profile_id: int, icp: dict, company_ids: list[str]) -> int:
    """(Re)score the given companies plus every company this profile has
    scored before, so a profile edit (new employee range, keywords, ...)
    re-evaluates its earlier discoveries too. Scoring is deterministic and
    free, so recomputing is cheaper than tracking staleness.

    Returns the number of companies scored.
    """
    conn = get_connection()
    try:
        previously_scored = [
            r["company_id"]
            for r in conn.execute(
                "SELECT company_id FROM company_scores WHERE profile_id=?", (profile_id,)
            )
        ]
        ids = list(dict.fromkeys([*company_ids, *previously_scored]))
        now = utc_now()
        scored = 0
        for company_id in ids:
            row = conn.execute("SELECT * FROM companies WHERE id=?", (company_id,)).fetchone()
            if row is None:
                continue
            scored += 1
            result = score_company(row, icp)
            conn.execute(
                """
                INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (profile_id, company_id) DO UPDATE SET
                    score=excluded.score, label=excluded.label,
                    breakdown_json=excluded.breakdown_json, scored_at=excluded.scored_at
                """,
                (profile_id, company_id, result.score, result.label, json.dumps(result.breakdown), now),
            )
        conn.commit()
    finally:
        conn.close()
    return scored
