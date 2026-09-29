"""Local web UI.

Server-rendered Jinja2 pages, with htmx for live bits (search progress,
cost estimates). Pages about leads live in lead_pages.py; this module holds
the app itself, campaigns, searches, email drafts and CSV export.

Meant to run on the operator's own machine only (bound to 127.0.0.1, no
login), so two guards matter: a Host check (blocks DNS-rebinding) and a
same-origin check on every state-changing request (blocks other websites
from submitting forms to localhost -- e.g. silently starting a search that
spends API credits).
"""

import io
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import ConfigError, load_config, validate_config
from ..contacts import ContactNotFoundError
from ..db import DEFAULT_PROFILE_PATH
from ..export import filename_slug, write_memos_csv
from ..outreach import (
    DraftNotFoundError,
    OutreachError,
    approve_draft,
    generate_draft,
    get_draft,
    list_drafts,
    mark_sent,
    update_draft,
)
from ..profiles import (
    InvalidProfileNameError,
    ProfileExistsError,
    ProfileNotFoundError,
    create_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from ..review import MemoNotFoundError, get_memo, refresh_signals
from ..runs import RunNotFoundError, fail_orphaned_runs, get_run, list_runs
from ..usage import usage_context
from . import runner
from .forms import FORM_ERROR, config_to_form, form_to_config, group_errors
from .lead_pages import MAX_DISCOVER_LIMIT, register_lead_pages
from .rendering import mailto_link
from .templating import WEB_DIR, templates

logger = logging.getLogger("sales_agent")

DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_IMPORT_BYTES = 1_000_000

DRAFT_NOTICES = {
    "written": "Email written. Check it, edit if needed, then approve it.",
    "saved": "Changes saved.",
    "approved": "Email approved. Copy it or open it in your email app, send it, then mark it as sent.",
    "sent": "Marked as sent.",
}


def _starter_config() -> dict:
    """Pre-fills the "new campaign" form; an empty skeleton if the starter
    file is missing or invalid.
    """
    try:
        return load_config(str(DEFAULT_PROFILE_PATH))
    except ConfigError:
        return {"product": {}, "icp": {}, "signal_taxonomy": {}}


def _unique_copy_name(name: str) -> str:
    existing = {p.name for p in list_profiles()}
    candidate, n = f"{name} (copy)", 2
    while candidate in existing:
        candidate, n = f"{name} (copy {n})", n + 1
    return candidate


def _parse_limit(text: str) -> int | None:
    try:
        limit = int(text)
    except ValueError:
        return None
    return limit if 1 <= limit <= MAX_DISCOVER_LIMIT else None


def create_app(*, allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        interrupted = fail_orphaned_runs()
        if interrupted:
            logger.warning("Marked %d interrupted run(s) from a previous server as failed", interrupted)
        yield

    app = FastAPI(title="Sales Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(allowed_hosts))
    app.mount("/static", StaticFiles(directory=str(WEB_DIR / "static")), name="static")

    @app.middleware("http")
    async def reject_cross_site_writes(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        if request.method not in SAFE_METHODS:
            source = request.headers.get("origin") or request.headers.get("referer")
            cross_site = request.headers.get("sec-fetch-site") == "cross-site"
            if cross_site or (source and urlparse(source).netloc != request.headers.get("host")):
                logger.warning("Rejected cross-site %s %s from %s", request.method, request.url.path, source)
                return Response("Cross-site request rejected", status_code=403)
        return await call_next(request)

    register_lead_pages(app)

    # ---------- campaigns (stored as profiles) ----------

    def render_profile_form(
        request: Request,
        *,
        profile_id: int | None,
        name: str,
        values: dict[str, str],
        errors: dict[str, list[str]] | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profile_form.html",
            {
                "profile_id": profile_id,
                "name": name,
                "values": values,
                "errors": errors or {},
                "form_error": FORM_ERROR,
            },
            status_code=status_code,
        )

    def render_profiles(
        request: Request, *, import_error: str | None = None, status_code: int = 200
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "profiles.html",
            {"profiles": list_profiles(), "import_error": import_error},
            status_code=status_code,
        )

    async def save_profile_from_form(request: Request, profile_id: int | None) -> Response:
        form = {k: v for k, v in (await request.form()).items() if isinstance(v, str)}
        name = form.get("name", "")
        base = get_profile(profile_id).config if profile_id is not None else _starter_config()
        config, errors = form_to_config(form, base)
        if not errors:
            try:
                validate_config(config)
                if profile_id is None:
                    profile = create_profile(name, config)
                else:
                    profile = update_profile(profile_id, name=name, config=config)
            except ConfigError as exc:
                errors = group_errors(exc.errors)
            except (InvalidProfileNameError, ProfileExistsError) as exc:
                errors = {"name": [str(exc)]}
            else:
                # signal rules may have changed: keep existing research labels consistent
                refresh_signals(profile.id)
                return RedirectResponse(f"/profiles?saved={profile.id}", status_code=303)
        return render_profile_form(
            request, profile_id=profile_id, name=name, values=form, errors=errors, status_code=422
        )

    @app.get("/profiles", response_class=HTMLResponse)
    def profiles_page(request: Request) -> HTMLResponse:
        return render_profiles(request)

    @app.get("/profiles/new", response_class=HTMLResponse)
    def new_profile_page(request: Request) -> HTMLResponse:
        values = config_to_form(_starter_config())
        return render_profile_form(request, profile_id=None, name="", values=values)

    @app.post("/profiles")
    async def create_profile_route(request: Request) -> Response:
        return await save_profile_from_form(request, None)

    @app.get("/profiles/{profile_id:int}/edit", response_class=HTMLResponse)
    def edit_profile_page(request: Request, profile_id: int) -> HTMLResponse:
        profile = get_profile(profile_id)
        return render_profile_form(
            request, profile_id=profile.id, name=profile.name, values=config_to_form(profile.config)
        )

    @app.post("/profiles/{profile_id:int}")
    async def update_profile_route(request: Request, profile_id: int) -> Response:
        get_profile(profile_id)  # 404 before reading the form
        return await save_profile_from_form(request, profile_id)

    @app.post("/profiles/{profile_id:int}/duplicate")
    def duplicate_profile(profile_id: int) -> RedirectResponse:
        source = get_profile(profile_id)
        duplicate = create_profile(_unique_copy_name(source.name), source.config)
        return RedirectResponse(f"/profiles/{duplicate.id}/edit", status_code=303)

    @app.get("/profiles/{profile_id:int}/export")
    def export_profile(profile_id: int) -> JSONResponse:
        profile = get_profile(profile_id)
        return JSONResponse(
            profile.config,
            headers={"Content-Disposition": f'attachment; filename="{filename_slug(profile.name)}.json"'},
        )

    @app.post("/profiles/import")
    async def import_profile(request: Request) -> Response:
        form = await request.form()
        name, upload = form.get("name"), form.get("file")
        if not isinstance(name, str) or not isinstance(upload, UploadFile):
            return render_profiles(request, import_error="Choose a JSON file and a name", status_code=422)
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            return render_profiles(request, import_error="File is too large for a campaign", status_code=422)
        try:
            config = json.loads(raw.decode("utf-8"))
            validate_config(config)
            profile = create_profile(name, config)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return render_profiles(request, import_error="That file is not valid JSON", status_code=422)
        except (ConfigError, InvalidProfileNameError, ProfileExistsError) as exc:
            return render_profiles(request, import_error=str(exc), status_code=422)
        return RedirectResponse(f"/profiles?saved={profile.id}", status_code=303)

    @app.get("/profiles/{profile_id:int}/memos.csv")
    def download_memos_csv(profile_id: int) -> Response:
        profile = get_profile(profile_id)
        buffer = io.StringIO()
        write_memos_csv(profile.id, buffer)
        # BOM so Excel detects UTF-8 (the stored signal labels contain emoji)
        return Response(
            ("﻿" + buffer.getvalue()).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={
                "Content-Disposition": f'attachment; filename="leads_{filename_slug(profile.name)}.csv"'
            },
        )

    # ---------- searches (runs) ----------

    def render_runs(
        request: Request,
        *,
        selected_profile_id: int | None = None,
        limit: str = "25",
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "runs.html",
            {
                "runs": list_runs(),
                "profiles": list_profiles(),
                "selected_profile_id": selected_profile_id,
                "limit": limit,
                "max_limit": MAX_DISCOVER_LIMIT,
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(request: Request, profile_id: int | None = None) -> HTMLResponse:
        return render_runs(request, selected_profile_id=profile_id)

    @app.post("/runs")
    async def start_run(request: Request) -> Response:
        form = await request.form()
        profile_text, limit_text = str(form.get("profile_id", "")), str(form.get("limit", ""))
        limit = _parse_limit(limit_text)
        selected = int(profile_text) if profile_text.isdigit() else None
        if selected is None or limit is None:
            return render_runs(
                request,
                selected_profile_id=selected,
                limit=limit_text,
                error=f"Pick a campaign and a number between 1 and {MAX_DISCOVER_LIMIT}",
                status_code=422,
            )
        profile = get_profile(selected)
        try:
            run_id = runner.start_background_run(profile, limit)
        except runner.RunInProgressError as exc:
            return render_runs(
                request, selected_profile_id=selected, limit=limit_text, error=str(exc), status_code=409
            )
        return RedirectResponse(f"/runs/{run_id}", status_code=303)

    @app.get("/runs/{run_id:int}", response_class=HTMLResponse)
    def run_page(request: Request, run_id: int) -> HTMLResponse:
        return templates.TemplateResponse(request, "run_detail.html", {"run": get_run(run_id)})

    @app.get("/runs/{run_id:int}/status", response_class=HTMLResponse)
    def run_status(request: Request, run_id: int) -> HTMLResponse:
        return templates.TemplateResponse(request, "_run_status.html", {"run": get_run(run_id)})

    # ---------- email drafts ----------

    def render_draft(
        request: Request,
        draft_id: int,
        *,
        error: str | None = None,
        status_code: int = 200,
    ) -> HTMLResponse:
        draft = get_draft(draft_id)
        memo = get_memo(draft.memo_id)
        return templates.TemplateResponse(
            request,
            "draft_detail.html",
            {
                "draft": draft,
                "memo": memo,
                "evidence": [e for e in memo.evidence if e["cited"]],
                "mailto": mailto_link(draft.contact_email, draft.subject, draft.body),
                "notice": DRAFT_NOTICES.get(request.query_params.get("done", "")),
                "error": error,
            },
            status_code=status_code,
        )

    @app.get("/drafts/{draft_id:int}", response_class=HTMLResponse)
    def draft_page(request: Request, draft_id: int) -> HTMLResponse:
        return render_draft(request, draft_id)

    @app.post("/drafts/{draft_id:int}")
    async def save_draft(request: Request, draft_id: int) -> Response:
        form = await request.form()
        try:
            update_draft(draft_id, subject=str(form.get("subject", "")), body=str(form.get("body", "")))
        except OutreachError as exc:
            return render_draft(request, draft_id, error=str(exc), status_code=409)
        return RedirectResponse(f"/drafts/{draft_id}?done=saved", status_code=303)

    @app.post("/drafts/{draft_id:int}/approve")
    def approve_draft_route(request: Request, draft_id: int) -> Response:
        try:
            approve_draft(draft_id)
        except OutreachError as exc:
            return render_draft(request, draft_id, error=str(exc), status_code=409)
        return RedirectResponse(f"/drafts/{draft_id}?done=approved", status_code=303)

    @app.post("/drafts/{draft_id:int}/sent")
    def mark_sent_route(request: Request, draft_id: int) -> Response:
        try:
            mark_sent(draft_id)
        except OutreachError as exc:
            return render_draft(request, draft_id, error=str(exc), status_code=409)
        return RedirectResponse(f"/drafts/{draft_id}?done=sent", status_code=303)

    @app.post("/drafts/{draft_id:int}/regenerate")
    def regenerate_draft(request: Request, draft_id: int) -> Response:
        draft = get_draft(draft_id)
        try:
            with usage_context("write_email", profile_id=draft.profile_id):
                generate_draft(draft.memo_id, draft.contact_id)
        except OutreachError as exc:
            return render_draft(request, draft_id, error=str(exc), status_code=409)
        except Exception as exc:  # external LLM call: the old draft stays
            logger.exception("Rewriting draft %d failed", draft_id)
            return render_draft(
                request, draft_id, error=f"Rewriting failed, the draft is unchanged: {exc}", status_code=502
            )
        return RedirectResponse(f"/drafts/{draft_id}?done=written", status_code=303)

    @app.get("/outreach", response_class=HTMLResponse)
    def outreach_page(request: Request, profile_id: int | None = None, status: str = "") -> HTMLResponse:
        status = status if status in ("draft", "approved", "sent") else ""
        return templates.TemplateResponse(
            request,
            "outreach.html",
            {
                "drafts": list_drafts(profile_id, status or None),
                "profiles": list_profiles(),
                "profile_id": profile_id,
                "status": status,
            },
        )

    # ---------- errors ----------

    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(request, "not_found.html", {"message": str(exc)}, status_code=404)

    for error in (
        ProfileNotFoundError,
        RunNotFoundError,
        MemoNotFoundError,
        ContactNotFoundError,
        DraftNotFoundError,
    ):
        app.add_exception_handler(error, not_found)

    return app
