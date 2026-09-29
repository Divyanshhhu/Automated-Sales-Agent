"""Deterministic ICP Fit scoring.

This is a plain weighted rule function over structured company attributes --
no LLM. ICP Fit is a separate axis from Signal Confidence (see confidence.py);
they must never be collapsed into one number.
"""
from .db import get_connection

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


def score_company(company_row, icp: dict) -> tuple[float, str]:
    min_emp, max_emp = icp["employee_range"]
    emp = _employee_score(company_row["employee_count"], min_emp, max_emp)
    loc = _location_score(
        company_row["city"], company_row["state"], company_row["country"], icp["geographies"]
    )
    ind = _industry_score(
        company_row["industry"], company_row["short_description"], icp["industry_keywords"]
    )
    score = (
        emp * WEIGHTS["employee"] + loc * WEIGHTS["location"] + ind * WEIGHTS["industry"]
    )
    if score >= 70:
        label = "High"
    elif score >= 40:
        label = "Medium"
    else:
        label = "Low"
    return round(score, 1), label


def score_all_pending(icp: dict) -> list[str]:
    """Score every company that doesn't have a score yet. Returns company_ids scored."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM companies WHERE icp_fit_score IS NULL"
    ).fetchall()
    scored_ids = []
    for row in rows:
        score, label = score_company(row, icp)
        conn.execute(
            "UPDATE companies SET icp_fit_score=?, icp_fit_label=? WHERE id=?",
            (score, label, row["id"]),
        )
        scored_ids.append(row["id"])
    conn.commit()
    conn.close()
    return scored_ids
