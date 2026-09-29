"""Leads: every company a campaign has found, where it stands, and what to
do next -- the data behind the Home dashboard and the Leads list.

A company's stage is derived, never stored, so it can't drift from the
facts it summarizes (its score, its research's review status, the emails
written to its people):

    not a fit  -- scored below the campaign's minimum, or you rejected it
    research   -- a good fit whose research hasn't been written yet
    review     -- research written, waiting for your decision
    approved   -- worth contacting; no email sent yet
    contacted  -- at least one email marked sent
"""

import json
from dataclasses import dataclass, field

from .confidence import DISPLAY_NAMES
from .db import get_connection
from .profiles import Profile
from .review import get_memo

STAGES = ("review", "approved", "contacted", "research", "notfit")
STAGE_NAMES = {
    "review": "To review",
    "approved": "Approved",
    "contacted": "Contacted",
    "research": "Being researched",
    "notfit": "Not a fit",
}


_LEGAL_SUFFIXES = (
    "limited", "ltd", "ltd.", "pvt", "pvt.", "private", "company", "co", "co.", "llp", "inc", "inc.",
)


def short_name(name: str) -> str:
    """A company name for lists and headings: trailing legal words dropped
    ("Tata Housing Development Company Limited" -> "Tata Housing Development").
    """
    words = name.split()
    while len(words) > 1 and words[-1].lower().strip(",") in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def fit_word(score: float | None, threshold: float) -> str:
    if score is None:
        return "Not scored"
    if score >= 70 and score >= threshold:
        return "Great fit"
    return "Good fit" if score >= threshold else "Weak fit"


@dataclass(frozen=True)
class Lead:
    company_id: str
    name: str
    domain: str | None
    location: str
    employee_count: int | None
    fit: float | None
    fit_word: str
    stage: str
    next_step: str
    memo_id: int | None = None
    signal_level: str | None = None
    signal_summary: str = ""
    why_not: str = ""  # for "not a fit": the plain reason

    @property
    def stage_name(self) -> str:
        return STAGE_NAMES[self.stage]

    @property
    def signal_name(self) -> str:
        return DISPLAY_NAMES[self.signal_level] if self.signal_level else ""


def _threshold(profile: Profile) -> float:
    return profile.config["icp"].get("min_icp_fit_score", 50)


def _why_not_a_fit(row: dict, icp: dict, breakdown: dict) -> str:
    reasons = []
    low, high = icp["employee_range"]
    count = row["employee_count"]
    if breakdown.get("employee", 30) < 30 and count is not None:
        reasons.append(f"{'larger' if count > high else 'smaller'} than your {low:,}–{high:,} range")
    if breakdown.get("location", 40) == 0:
        reasons.append("outside your locations")
    if breakdown.get("industry", 30) == 0:
        reasons.append("description doesn't match your industry keywords")
    return "; ".join(reasons).capitalize() or "Scored below your minimum"


def list_leads(profile: Profile) -> list[Lead]:
    """Every company the campaign has scored, best first within each stage."""
    icp = profile.config["icp"]
    threshold = _threshold(profile)
    conn = get_connection()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT c.*, COALESCE(s.score, m.icp_fit_score) AS score, s.breakdown_json,
                       m.id AS memo_id, m.review_status,
                       (SELECT COUNT(*) FROM contacts ct WHERE ct.company_id = c.id) AS contact_count,
                       (SELECT COUNT(*) FROM outreach_drafts d WHERE d.memo_id = m.id) AS draft_count,
                       (SELECT COUNT(*) FROM outreach_drafts d
                        WHERE d.memo_id = m.id AND d.status = 'draft')
                           AS drafts_to_check,
                       (SELECT COUNT(*) FROM outreach_drafts d
                        WHERE d.memo_id = m.id AND d.status = 'approved')
                           AS drafts_ready,
                       (SELECT COUNT(*) FROM outreach_drafts d
                        WHERE d.memo_id = m.id AND d.status = 'sent')
                           AS drafts_sent
                FROM companies c
                -- every company the campaign scored or researched
                JOIN (
                    SELECT company_id FROM company_scores WHERE profile_id = :p
                    UNION SELECT company_id FROM memos WHERE profile_id = :p
                ) ids ON ids.company_id = c.id
                LEFT JOIN company_scores s ON s.profile_id = :p AND s.company_id = c.id
                LEFT JOIN memos m ON m.profile_id = :p AND m.company_id = c.id
                ORDER BY score DESC, c.name COLLATE NOCASE
                """,
                {"p": profile.id},
            )
        ]
    finally:
        conn.close()

    leads = []
    for r in rows:
        location = ", ".join(v for v in (r["city"], r["country"]) if v)
        base = {
            "company_id": r["id"],
            "name": r["name"],
            "domain": r["domain"],
            "location": location,
            "employee_count": r["employee_count"],
            "fit": r["score"],
            "fit_word": fit_word(r["score"], threshold),
            "memo_id": r["memo_id"],
        }
        if r["memo_id"] is None:
            if r["score"] is not None and r["score"] >= threshold:
                leads.append(Lead(**base, stage="research", next_step="Researched on the next search"))
            else:
                breakdown = json.loads(r["breakdown_json"]) if r["breakdown_json"] else {}
                leads.append(
                    Lead(
                        **base, stage="notfit", next_step="Skipped", why_not=_why_not_a_fit(r, icp, breakdown)
                    )
                )
            continue

        memo = get_memo(r["memo_id"])
        level, summary = memo.signal.level, memo.signal.summary
        status = r["review_status"]
        if status == "rejected":
            stage, step = "notfit", "Skipped"
            leads.append(
                Lead(
                    **base,
                    signal_level=level,
                    signal_summary=summary,
                    stage=stage,
                    next_step=step,
                    why_not="You marked it not a fit",
                )
            )
            continue
        if status in ("pending", "needs_review"):
            stage, step = "review", "Review"
        elif r["drafts_sent"]:
            stage, step = "contacted", "Contacted"
        elif r["drafts_ready"]:
            stage, step = "approved", "Send email"
        elif r["drafts_to_check"]:
            stage, step = "approved", "Check email"
        elif r["contact_count"]:
            stage, step = "approved", "Write email"
        else:
            stage, step = "approved", "Find contacts"
        leads.append(Lead(**base, signal_level=level, signal_summary=summary, stage=stage, next_step=step))
    return leads


def stage_counts(leads: list[Lead]) -> dict[str, int]:
    counts = {stage: 0 for stage in STAGES}
    for lead in leads:
        counts[lead.stage] += 1
    counts["all"] = len(leads)
    return counts


def review_queue(profile: Profile) -> list[int]:
    """Memo ids waiting for a decision, in the order Leads lists them."""
    return [lead.memo_id for lead in list_leads(profile) if lead.stage == "review" and lead.memo_id]


# ---------- Home ----------


@dataclass(frozen=True)
class Pipeline:
    found: int
    good_fit: int
    to_review: int
    approved: int
    emails_ready: int
    sent: int


@dataclass(frozen=True)
class NextStep:
    title: str
    detail: str
    href: str
    button: str
    primary: bool = False


@dataclass(frozen=True)
class Dashboard:
    pipeline: Pipeline
    next_steps: list[NextStep]
    searched_locations: list[str]
    unsearched_locations: list[str]
    leads: list[Lead] = field(default_factory=list)


def _draft_counts(profile_id: int) -> dict[str, int]:
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT d.status, COUNT(*) AS n FROM outreach_drafts d JOIN memos m ON m.id = d.memo_id
            WHERE m.profile_id = ? GROUP BY d.status
            """,
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()
    return {r["status"]: r["n"] for r in rows}


def _first_draft(profile_id: int, status: str) -> int | None:
    conn = get_connection()
    try:
        row = conn.execute(
            """
            SELECT d.id FROM outreach_drafts d JOIN memos m ON m.id = d.memo_id
            WHERE m.profile_id = ? AND d.status = ? ORDER BY d.updated_at LIMIT 1
            """,
            (profile_id, status),
        ).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


def searched_locations(profile: Profile, leads: list[Lead]) -> list[str]:
    """Locations searched by this campaign: recorded by each run since that
    was tracked, plus -- for older runs -- any configured city where a found
    company is based.
    """
    conn = get_connection()
    try:
        stats = [
            r["stats_json"]
            for r in conn.execute("SELECT stats_json FROM runs WHERE profile_id=?", (profile.id,))
        ]
    finally:
        conn.close()
    searched: set[str] = set()
    for raw in stats:
        if raw:
            searched.update(json.loads(raw).get("searched_locations", []))
    cities = {lead.location.split(",")[0].strip().lower() for lead in leads if lead.location}
    geographies = profile.config["icp"]["geographies"]
    searched.update(g for g in geographies if g.lower() in cities)
    return [g for g in geographies if g in searched]


def _count(n: int, singular: str, plural: str) -> str:
    """ "1 company has" / "3 companies have" """
    return f"{n} {singular if n == 1 else plural}"


def _names(leads: list[Lead], limit: int = 3) -> str:
    names = [short_name(lead.name) for lead in leads[:limit]]
    more = len(leads) - limit
    return ", ".join(names) + (f" and {more} more" if more > 0 else "")


def dashboard(profile: Profile) -> Dashboard:
    leads = list_leads(profile)
    drafts = _draft_counts(profile.id)
    by_stage = {stage: [lead for lead in leads if lead.stage == stage] for stage in STAGES}
    threshold = _threshold(profile)
    pipeline = Pipeline(
        found=len(leads),
        good_fit=sum(1 for lead in leads if lead.fit is not None and lead.fit >= threshold),
        to_review=len(by_stage["review"]),
        approved=len(by_stage["approved"]),
        emails_ready=drafts.get("approved", 0),
        sent=drafts.get("sent", 0),
    )

    steps: list[NextStep] = []
    if by_stage["review"]:
        first = by_stage["review"][0]
        count = len(by_stage["review"])
        steps.append(
            NextStep(
                f"{count} compan{'y is' if count == 1 else 'ies are'} waiting for your review",
                _names(by_stage["review"]) + " — about a minute each",
                f"/leads/{first.memo_id}",
                "Start reviewing",
                primary=True,
            )
        )
    unchecked = [
        lead
        for lead in by_stage["review"] + by_stage["approved"]
        if lead.memo_id and get_memo(lead.memo_id).unchecked_sources
    ]
    if unchecked:
        sources = get_memo(unchecked[0].memo_id or 0).unchecked_sources
        steps.append(
            NextStep(
                f"{_count(len(unchecked), 'company has', 'companies have')} sources never checked",
                f"{_names(unchecked)}: {', '.join(sources)}. Updating the research checks them.",
                f"/leads/{unchecked[0].memo_id}",
                "Open",
            )
        )
    need_contacts = [lead for lead in by_stage["approved"] if lead.next_step == "Find contacts"]
    if need_contacts:
        steps.append(
            NextStep(
                _count(len(need_contacts), "approved company has", "approved companies have")
                + " no contact yet",
                _names(need_contacts),
                f"/leads/{need_contacts[0].memo_id}",
                "Find contacts",
            )
        )
    if drafts.get("draft") and (draft_id := _first_draft(profile.id, "draft")):
        steps.append(
            NextStep(
                f"{drafts['draft']} email{'s' if drafts['draft'] != 1 else ''} to check",
                "Written and waiting for your approval",
                f"/drafts/{draft_id}",
                "Check email",
            )
        )
    if drafts.get("approved") and (draft_id := _first_draft(profile.id, "approved")):
        steps.append(
            NextStep(
                f"{drafts['approved']} email{'s' if drafts['approved'] != 1 else ''} ready to send",
                "Approved — send from your own email, then mark as sent",
                f"/drafts/{draft_id}",
                "Send email",
            )
        )

    searched = searched_locations(profile, leads)
    geographies = profile.config["icp"]["geographies"]
    unsearched = [g for g in geographies if g not in searched]
    if unsearched:
        steps.append(
            NextStep(
                f"{len(unsearched)} of your {len(geographies)} locations haven't been searched yet",
                ("Searched so far: " + ", ".join(searched) + ". " if searched else "")
                + "Next: "
                + ", ".join(unsearched)
                + ".",
                "#find",
                "Find leads",
            )
        )
    return Dashboard(pipeline, steps, searched, unsearched, leads)
