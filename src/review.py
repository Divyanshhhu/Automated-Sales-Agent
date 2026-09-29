"""Human review of memos: listing, reading with evidence, approving or
rejecting, and regenerating.

Approved memos are the only input to outreach drafting (Milestone 4), so
every decision is recorded with its status, the reviewer's notes, and when
it was made.
"""
import json
from dataclasses import dataclass

from .confidence import LABELS
from .db import get_connection, utc_now
from .evidence import get_evidence_for_company
from .memo_generator import extract_cited_evidence_ids, write_memo
from .profiles import get_profile

MAX_NOTES_LENGTH = 2000

# status filter -> the review_status values it covers
STATUS_FILTERS: dict[str, tuple[str, ...]] = {
    "to_review": ("pending", "needs_review"),
    "approved": ("approved",),
    "rejected": ("rejected",),
    "all": ("pending", "needs_review", "approved", "rejected"),
}
CONFIDENCE_FILTERS = {key: LABELS[key] for key in ("strong", "plausible", "weak")}

_CONFIDENCE_RANK = (
    f"CASE m.signal_confidence WHEN '{LABELS['strong']}' THEN 0 "
    f"WHEN '{LABELS['plausible']}' THEN 1 ELSE 2 END"
)
# Whitelisted ORDER BY clauses -- user input only ever selects a key.
SORTS = {
    "score": f"m.icp_fit_score DESC, {_CONFIDENCE_RANK}, c.name COLLATE NOCASE",
    "confidence": f"{_CONFIDENCE_RANK}, m.icp_fit_score DESC, c.name COLLATE NOCASE",
    "company": "c.name COLLATE NOCASE",
    "newest": "m.generated_at DESC, m.id DESC",
}
DECISIONS = ("approved", "rejected", "reset")


class MemoNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class MemoSummary:
    id: int
    company_name: str
    domain: str | None
    location: str
    icp_fit_score: float | None
    icp_fit_label: str | None
    signal_confidence: str | None
    review_status: str
    issue_count: int
    generated_at: str | None


@dataclass(frozen=True)
class MemoDetail:
    id: int
    profile_id: int
    profile_name: str
    run_id: int | None
    company: dict
    icp_fit_score: float | None
    icp_fit_label: str | None
    current_score: float | None  # the company's score under the profile now; may differ after edits
    score_breakdown: dict[str, float]
    signal_confidence: str | None
    why_relevant: str
    potential_use_case: str
    issues: list[str]
    review_status: str
    review_notes: str | None
    reviewed_at: str | None
    generated_at: str | None
    evidence: list[dict]  # each item gains "cited": bool

    @property
    def evidence_ids(self) -> set[int]:
        return {e["id"] for e in self.evidence}


def _location(city: str | None, state: str | None, country: str | None) -> str:
    return ", ".join(v for v in (city, state, country) if v)


def _issues(raw: str | None) -> list[str]:
    return json.loads(raw) if raw else []


def list_memos(
    profile_id: int, *, status: str = "to_review", confidence: str | None = None, sort: str = "score"
) -> list[MemoSummary]:
    statuses = STATUS_FILTERS.get(status, STATUS_FILTERS["to_review"])
    params: list[object] = [profile_id, *statuses]
    where = f"m.profile_id = ? AND m.review_status IN ({','.join('?' * len(statuses))})"
    if confidence in CONFIDENCE_FILTERS:
        where += " AND m.signal_confidence = ?"
        params.append(CONFIDENCE_FILTERS[confidence])
    order = SORTS.get(sort, SORTS["score"])
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT m.*, c.name AS company_name, c.domain, c.city, c.state, c.country
            FROM memos m JOIN companies c ON c.id = m.company_id
            WHERE {where} ORDER BY {order}
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    return [
        MemoSummary(
            id=r["id"],
            company_name=r["company_name"],
            domain=r["domain"],
            location=_location(r["city"], r["state"], r["country"]),
            icp_fit_score=r["icp_fit_score"],
            icp_fit_label=r["icp_fit_label"],
            signal_confidence=r["signal_confidence"],
            review_status=r["review_status"],
            issue_count=len(_issues(r["citation_issues"])),
            generated_at=r["generated_at"],
        )
        for r in rows
    ]


def status_counts(profile_id: int) -> dict[str, int]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT review_status, COUNT(*) AS n FROM memos WHERE profile_id=? GROUP BY review_status",
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()
    by_status = {r["review_status"]: r["n"] for r in rows}
    return {name: sum(by_status.get(s, 0) for s in statuses) for name, statuses in STATUS_FILTERS.items()}


def get_memo(memo_id: int) -> MemoDetail:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT m.*, p.name AS profile_name, s.score AS current_score, s.breakdown_json,
                   c.name AS company_name, c.domain, c.employee_count, c.city, c.state, c.country,
                   c.short_description
            FROM memos m
            JOIN profiles p ON p.id = m.profile_id
            JOIN companies c ON c.id = m.company_id
            LEFT JOIN company_scores s ON s.profile_id = m.profile_id AND s.company_id = m.company_id
            WHERE m.id = ?
            """,
            (memo_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise MemoNotFoundError(f"No memo with id {memo_id}")

    why, use_case = row["why_relevant_text"] or "", row["potential_use_case_text"] or ""
    cited = extract_cited_evidence_ids(why) | extract_cited_evidence_ids(use_case)
    shown_ids = set(json.loads(row["evidence_ids_json"])) if row["evidence_ids_json"] else None
    evidence = [
        {**e, "cited": e["id"] in cited}
        for e in get_evidence_for_company(row["profile_id"], row["company_id"])
        # only what the model was shown for this memo (older memos: everything)
        if shown_ids is None or e["id"] in shown_ids
    ]
    return MemoDetail(
        id=row["id"],
        profile_id=row["profile_id"],
        profile_name=row["profile_name"],
        run_id=row["run_id"],
        company={
            "id": row["company_id"],
            "name": row["company_name"],
            "domain": row["domain"],
            "employee_count": row["employee_count"],
            "location": _location(row["city"], row["state"], row["country"]),
            "description": row["short_description"],
        },
        icp_fit_score=row["icp_fit_score"],
        icp_fit_label=row["icp_fit_label"],
        current_score=row["current_score"],
        score_breakdown=json.loads(row["breakdown_json"]) if row["breakdown_json"] else {},
        signal_confidence=row["signal_confidence"],
        why_relevant=why,
        potential_use_case=use_case,
        issues=_issues(row["citation_issues"]),
        review_status=row["review_status"],
        review_notes=row["review_notes"],
        reviewed_at=row["reviewed_at"],
        generated_at=row["generated_at"],
        evidence=evidence,
    )


def set_review(memo_id: int, decision: str, notes: str = "") -> str:
    """Records a decision ("approved" / "rejected"), or "reset" to undo one.
    Returns the memo's new review_status.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    memo = get_memo(memo_id)
    cleaned_notes = notes.strip()[:MAX_NOTES_LENGTH] or None
    if decision == "reset":
        new_status = "needs_review" if memo.issues else "pending"
        reviewed_at = None
    else:
        new_status, reviewed_at = decision, utc_now()
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE memos SET review_status=?, review_notes=?, reviewed_at=? WHERE id=?",
            (new_status, cleaned_notes, reviewed_at, memo_id),
        )
        conn.commit()
    finally:
        conn.close()
    return new_status


def next_memo_to_review(profile_id: int, *, exclude_id: int | None = None) -> int | None:
    """The next memo still waiting for a decision, in the default list order."""
    for memo in list_memos(profile_id, status="to_review"):
        if memo.id != exclude_id:
            return memo.id
    return None


def regenerate_memo(memo_id: int) -> MemoDetail:
    """Rewrites the memo from the same stored evidence, using the profile's
    *current* product description, and re-runs the citation checks. The
    previous decision is cleared (the text it judged is gone); notes are
    kept, since they often say what was wrong. Costs one LLM call.
    """
    memo = get_memo(memo_id)
    profile = get_profile(memo.profile_id)
    conn = get_connection()
    try:
        company_row = conn.execute(
            """
            SELECT c.*, COALESCE(s.score, ?) AS icp_fit_score, COALESCE(s.label, ?) AS icp_fit_label
            FROM companies c
            LEFT JOIN company_scores s ON s.company_id = c.id AND s.profile_id = ?
            WHERE c.id = ?
            """,
            (memo.icp_fit_score, memo.icp_fit_label, memo.profile_id, memo.company["id"]),
        ).fetchone()
    finally:
        conn.close()
    evidence_items = get_evidence_for_company(memo.profile_id, memo.company["id"])

    content = write_memo(profile.config["product"], company_row, evidence_items)

    conn = get_connection()
    try:
        conn.execute(
            """
            UPDATE memos SET
                icp_fit_score=?, icp_fit_label=?, signal_confidence=?,
                why_relevant_text=?, potential_use_case_text=?, evidence_ids_json=?,
                citation_issues=?, review_status=?, reviewed_at=NULL, generated_at=?
            WHERE id=?
            """,
            (
                company_row["icp_fit_score"],
                company_row["icp_fit_label"],
                content.signal_confidence,
                content.why_relevant,
                content.potential_use_case,
                json.dumps(content.evidence_ids),
                json.dumps(content.issues) if content.issues else None,
                content.review_status,
                utc_now(),
                memo_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return get_memo(memo_id)
