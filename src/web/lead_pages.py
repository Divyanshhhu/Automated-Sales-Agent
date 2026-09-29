"""Home, Leads and the company page -- plus every action taken on a company:
decide, update its research, find its people, write an email.

Each action that calls a paid API runs inside a usage context naming the
action and campaign, so its real cost is recorded (see usage.py).
"""

import logging

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.datastructures import FormData

from ..contacts import (
    ContactValidationError,
    add_manual_contact,
    delete_contact,
    find_people,
    get_contact,
    list_contacts,
    update_contact,
)
from ..icp_scorer import WEIGHTS
from ..leads import STAGE_NAMES, STAGES, dashboard, list_leads, review_queue, stage_counts
from ..outreach import OutreachError, drafts_for_memo, generate_draft
from ..profiles import Profile, get_profile, list_profiles
from ..review import DECISIONS, MemoDetail, get_memo, set_review, update_research
from ..runs import list_runs
from ..usage import (
    EXA_FALLBACK_PER_SEARCH,
    OPENAI_COST_PER_MEMO,
    TAVILY_FREE_CREDITS_PER_MONTH,
    TAVILY_PRICE_PER_CREDIT,
    estimate_run,
    month_cost,
    usage_context,
)
from .rendering import render_research
from .templating import templates

logger = logging.getLogger("sales_agent")

MAX_DISCOVER_LIMIT = 100
DEFAULT_DISCOVER_LIMIT = 25

NOTICES = {
    "approved": "Approved.",
    "reset": "Decision undone.",
    "updated": "Research updated.",
    "added": "Person added.",
    "saved": "Person updated.",
    "deleted": "Person deleted, along with any emails written to them.",
}


def _campaign(campaign_id: int | None) -> Profile | None:
    """The campaign a page is about: the one asked for, else the one used for
    the most recent search, else the first.
    """
    if campaign_id is not None:
        return get_profile(campaign_id)
    for run in list_runs(limit=1):
        return get_profile(run.profile_id)
    profiles = list_profiles()
    return profiles[0] if profiles else None


def _contact_fields(form: FormData) -> dict[str, str]:
    return {key: str(form.get(key, "")) for key in ("name", "title", "email", "profile_url")}


def _people_notice(added: str, known: str) -> str:
    also = f" ({known} already saved)" if known not in ("", "0") else ""
    if added in ("", "0"):
        return f"No new people found in the target roles{also}. You can add someone yourself."
    return f"Found {added} {'person' if added == '1' else 'people'} to contact{also}."


# ---------- the company page's pieces ----------

PROGRESS = ("Research", "Approve", "Find contact", "Write email", "Sent")
_REACHED = {
    "Review": 0,
    "Find contacts": 1,
    "Write email": 2,
    "Check email": 3,
    "Send email": 3,
    "Contacted": 4,
}


def _sources(memo: MemoDetail) -> tuple[dict[int, int], list[dict], list[dict]]:
    """Footnote numbers in order of first citation, the cited sources in that
    order, and the other sources found for the company.
    """
    order: list[int] = []
    text = f"{memo.why_relevant} {memo.potential_use_case}"
    for part in text.split("[E")[1:]:
        digits = part.split("]", 1)[0]
        if digits.isdigit() and int(digits) in memo.evidence_ids and int(digits) not in order:
            order.append(int(digits))
    numbers = {evidence_id: i + 1 for i, evidence_id in enumerate(order)}
    by_id = {e["id"]: e for e in memo.evidence}
    cited = [{**by_id[i], "number": numbers[i]} for i in order]
    others = [e for e in memo.evidence if e["id"] not in numbers]
    return numbers, cited, others


def _research_banner(memo: MemoDetail, others: list[dict]) -> dict | None:
    """Why and how to update the research, when that would help."""
    skipped_complaints = [e for e in others if e["category"] == "direct_pain_point"]
    if memo.unchecked_sources:
        cost = len(memo.unchecked_sources) * TAVILY_PRICE_PER_CREDIT + OPENAI_COST_PER_MEMO
        return {
            "title": f"Not checked yet for this company: {', '.join(memo.unchecked_sources)}",
            "detail": f"Updating runs those searches and rewrites the research — about ${cost:.2f}.",
        }
    if skipped_complaints:
        return {
            "title": "A customer complaint was found but isn't in the research yet",
            "detail": f"Updating rewrites the research to include it — about ${OPENAI_COST_PER_MEMO:.3f}.",
        }
    return None


def render_lead(
    request: Request, memo_id: int, *, error: str | None = None, status_code: int = 200
) -> HTMLResponse:
    memo = get_memo(memo_id)
    campaign = get_profile(memo.profile_id)
    lead = next((lead for lead in list_leads(campaign) if lead.memo_id == memo_id), None)
    queue = review_queue(campaign)
    position = queue.index(memo_id) if memo_id in queue else -1
    others_in_queue = [m for m in queue if m != memo_id]
    next_id = (
        queue[position + 1]
        if 0 <= position < len(queue) - 1
        else (others_in_queue[0] if others_in_queue else None)
    )
    prev_id = queue[position - 1] if position > 0 else None

    numbers, cited, others = _sources(memo)
    why = render_research(memo.why_relevant, numbers)
    use = render_research(memo.potential_use_case, numbers)
    rejected = memo.review_status == "rejected"
    reached = 0 if rejected or lead is None else _REACHED.get(lead.next_step, 0)

    params = request.query_params
    notice = NOTICES.get(params.get("done", ""))
    if params.get("people") is not None:
        notice = _people_notice(params.get("people", "0"), params.get("known", "0"))
    if params.get("done") == "updated" and params.get("new"):
        notice = f"Research updated — {params['new']} new source(s) found."
    next_memo = get_memo(next_id) if next_id else None

    return templates.TemplateResponse(
        request,
        "lead_detail.html",
        {
            "memo": memo,
            "lead": lead,
            "campaign": campaign,
            "progress": [
                {"label": label, "done": i <= reached, "current": i == reached + 1 and not rejected}
                for i, label in enumerate(PROGRESS)
            ],
            "rejected": rejected,
            "why": why,
            "use": use,
            "gaps": why.gaps + use.gaps,
            "cited": cited,
            "others": others,
            "banner": _research_banner(memo, others),
            "contacts": list_contacts(memo.company["id"]),
            "drafts": drafts_for_memo(memo.id),
            "weights": WEIGHTS,
            "roles": campaign.config["icp"].get("roles") or [],
            "next_memo": next_memo,
            "next_id": next_id,
            "prev_id": prev_id,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


def register_lead_pages(app: FastAPI) -> None:
    # ---------- Home ----------

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, campaign: int | None = None) -> HTMLResponse:
        current = _campaign(campaign)
        runs = [r for r in list_runs(limit=20) if current and r.profile_id == current.id][:3]
        return templates.TemplateResponse(
            request,
            "home.html",
            {
                "campaigns": list_profiles(),
                "campaign": current,
                "board": dashboard(current) if current else None,
                "estimate": estimate_run(DEFAULT_DISCOVER_LIMIT),
                "default_limit": DEFAULT_DISCOVER_LIMIT,
                "max_limit": MAX_DISCOVER_LIMIT,
                "month": month_cost(),
                "free_credits": TAVILY_FREE_CREDITS_PER_MONTH,
                "runs": runs,
                "active_run": next((r for r in list_runs(limit=5) if r.is_active), None),
            },
        )

    @app.get("/estimate", response_class=HTMLResponse)
    def estimate(request: Request, limit: str = "") -> HTMLResponse:
        n = int(limit) if limit.isdigit() else DEFAULT_DISCOVER_LIMIT
        n = max(1, min(MAX_DISCOVER_LIMIT, n))
        return templates.TemplateResponse(
            request,
            "_estimate.html",
            {"estimate": estimate_run(n), "count": n, "exa_per_search": EXA_FALLBACK_PER_SEARCH},
        )

    # ---------- Leads ----------

    @app.get("/leads", response_class=HTMLResponse)
    def leads_page(
        request: Request, campaign: int | None = None, stage: str = "", q: str = ""
    ) -> HTMLResponse:
        current = _campaign(campaign)
        leads = list_leads(current) if current else []
        counts = stage_counts(leads)
        if stage not in (*STAGES, "all"):
            stage = "review" if counts["review"] else "all"
        needle = q.strip().lower()
        shown = [
            lead
            for lead in leads
            if (stage == "all" or lead.stage == stage) and (not needle or needle in lead.name.lower())
        ]
        return templates.TemplateResponse(
            request,
            "leads.html",
            {
                "campaigns": list_profiles(),
                "campaign": current,
                "leads": shown,
                "counts": counts,
                "stage": stage,
                "stages": [
                    ("all", "All"),
                    *[(s, STAGE_NAMES[s]) for s in STAGES if s != "research" or counts[s]],
                ],
                "q": q,
            },
        )

    @app.get("/review")
    def old_review_page() -> RedirectResponse:
        return RedirectResponse("/leads", status_code=301)

    # ---------- the company page ----------

    @app.get("/leads/{memo_id:int}", response_class=HTMLResponse)
    def lead_page(request: Request, memo_id: int) -> HTMLResponse:
        return render_lead(request, memo_id)

    @app.get("/memos/{memo_id:int}")
    def old_memo_page(memo_id: int) -> RedirectResponse:
        return RedirectResponse(f"/leads/{memo_id}", status_code=301)

    @app.post("/leads/{memo_id:int}/decision")
    async def decide(request: Request, memo_id: int) -> Response:
        form = await request.form()
        decision, notes = str(form.get("decision", "")), str(form.get("notes", ""))
        if decision not in DECISIONS:
            return render_lead(request, memo_id, error="Unknown decision", status_code=422)
        set_review(memo_id, decision, notes)
        if decision == "rejected":
            # nothing more to do here: carry on with the queue
            queue = review_queue(get_profile(get_memo(memo_id).profile_id))
            if queue:
                return RedirectResponse(f"/leads/{queue[0]}", status_code=303)
            return RedirectResponse("/leads?stage=review", status_code=303)
        done = "approved" if decision == "approved" else "reset"
        return RedirectResponse(f"/leads/{memo_id}?done={done}", status_code=303)

    @app.post("/leads/{memo_id:int}/update-research")
    def update_research_route(request: Request, memo_id: int) -> Response:
        memo = get_memo(memo_id)  # 404 before spending anything
        try:
            with usage_context("update_research", profile_id=memo.profile_id):
                result = update_research(memo_id)
        except Exception as exc:  # external calls: show the failure, keep the old research
            logger.exception("Updating research for memo %d failed", memo_id)
            return render_lead(
                request, memo_id, error=f"Updating failed; the research is unchanged: {exc}", status_code=502
            )
        return RedirectResponse(f"/leads/{memo_id}?done=updated&new={result.new_sources}", status_code=303)

    # ---------- people ----------

    @app.post("/leads/{memo_id:int}/find-people")
    def find_people_route(request: Request, memo_id: int) -> Response:
        memo = get_memo(memo_id)
        if memo.review_status != "approved":
            return render_lead(
                request, memo_id, error="Approve the company before finding people", status_code=409
            )
        roles = get_profile(memo.profile_id).config["icp"].get("roles") or []
        try:
            with usage_context("find_people", profile_id=memo.profile_id):
                result = find_people(memo.company["id"], memo.company["name"], memo.company["city"], roles)
        except Exception as exc:  # external search: show the failure, change nothing
            logger.exception("Finding people for memo %d failed", memo_id)
            return render_lead(request, memo_id, error=f"People search failed: {exc}", status_code=502)
        return RedirectResponse(
            f"/leads/{memo_id}?people={len(result.added)}&known={result.already_saved}#people",
            status_code=303,
        )

    @app.post("/leads/{memo_id:int}/contacts")
    async def add_contact_route(request: Request, memo_id: int) -> Response:
        memo = get_memo(memo_id)
        try:
            add_manual_contact(memo.company["id"], **_contact_fields(await request.form()))
        except ContactValidationError as exc:
            return render_lead(request, memo_id, error=str(exc), status_code=422)
        return RedirectResponse(f"/leads/{memo_id}?done=added#people", status_code=303)

    @app.post("/leads/{memo_id:int}/contacts/{contact_id:int}")
    async def update_contact_route(request: Request, memo_id: int, contact_id: int) -> Response:
        get_memo(memo_id)
        try:
            update_contact(contact_id, **_contact_fields(await request.form()))
        except ContactValidationError as exc:
            return render_lead(request, memo_id, error=str(exc), status_code=422)
        return RedirectResponse(f"/leads/{memo_id}?done=saved#people", status_code=303)

    @app.post("/leads/{memo_id:int}/contacts/{contact_id:int}/delete")
    def delete_contact_route(memo_id: int, contact_id: int) -> Response:
        get_memo(memo_id)
        delete_contact(contact_id)
        return RedirectResponse(f"/leads/{memo_id}?done=deleted#people", status_code=303)

    @app.post("/leads/{memo_id:int}/contacts/{contact_id:int}/draft")
    def write_draft(request: Request, memo_id: int, contact_id: int) -> Response:
        memo = get_memo(memo_id)
        get_contact(contact_id)  # 404 before spending an LLM call
        try:
            with usage_context("write_email", profile_id=memo.profile_id):
                draft = generate_draft(memo_id, contact_id)
        except OutreachError as exc:
            return render_lead(request, memo_id, error=str(exc), status_code=409)
        except Exception as exc:  # external LLM call
            logger.exception("Writing email for memo %d, contact %d failed", memo_id, contact_id)
            return render_lead(request, memo_id, error=f"Writing the email failed: {exc}", status_code=502)
        return RedirectResponse(f"/drafts/{draft.id}?done=written", status_code=303)
