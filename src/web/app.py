"""Local web UI: profile editor + run launcher with live progress.

Server-rendered Jinja2 pages, with htmx polling for run progress. Meant to
run on the operator's own machine only (bound to 127.0.0.1, no login), so two
guards matter: a Host check (blocks DNS-rebinding) and a same-origin check on
every state-changing request (blocks other websites from submitting forms to
localhost -- e.g. silently starting a run that spends API credits).
"""
import json
import logging
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.datastructures import UploadFile
from starlette.middleware.trustedhost import TrustedHostMiddleware

from ..config import ConfigError, load_config, validate_config
from ..db import DEFAULT_PROFILE_PATH
from ..profiles import (
    InvalidProfileNameError,
    ProfileExistsError,
    ProfileNotFoundError,
    create_profile,
    get_profile,
    list_profiles,
    update_profile,
)
from ..runs import RunNotFoundError, fail_orphaned_runs, get_run, list_runs
from . import runner
from .forms import FORM_ERROR, config_to_form, form_to_config, group_errors

logger = logging.getLogger("sales_agent")

WEB_DIR = Path(__file__).resolve().parent
DEFAULT_ALLOWED_HOSTS = ("127.0.0.1", "localhost")
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_DISCOVER_LIMIT = 100
MAX_IMPORT_BYTES = 1_000_000

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profile"


def _starter_config() -> dict:
    """Pre-fills the "new profile" form; an empty skeleton if the starter
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

    # ---------- profiles ----------

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
                return RedirectResponse(f"/profiles?saved={profile.id}", status_code=303)
        return render_profile_form(
            request, profile_id=profile_id, name=name, values=form, errors=errors, status_code=422
        )

    @app.get("/")
    def home() -> RedirectResponse:
        return RedirectResponse("/profiles", status_code=303)

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
            headers={"Content-Disposition": f'attachment; filename="{_slug(profile.name)}.json"'},
        )

    @app.post("/profiles/import")
    async def import_profile(request: Request) -> Response:
        form = await request.form()
        name, upload = form.get("name"), form.get("file")
        if not isinstance(name, str) or not isinstance(upload, UploadFile):
            return render_profiles(request, import_error="Choose a JSON file and a name", status_code=422)
        raw = await upload.read(MAX_IMPORT_BYTES + 1)
        if len(raw) > MAX_IMPORT_BYTES:
            return render_profiles(request, import_error="File is too large for a profile", status_code=422)
        try:
            config = json.loads(raw.decode("utf-8"))
            validate_config(config)
            profile = create_profile(name, config)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return render_profiles(request, import_error="That file is not valid JSON", status_code=422)
        except (ConfigError, InvalidProfileNameError, ProfileExistsError) as exc:
            return render_profiles(request, import_error=str(exc), status_code=422)
        return RedirectResponse(f"/profiles?saved={profile.id}", status_code=303)

    # ---------- runs ----------

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
                error=f"Pick a profile and a limit between 1 and {MAX_DISCOVER_LIMIT}",
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

    # ---------- errors ----------

    async def not_found(request: Request, exc: Exception) -> HTMLResponse:
        return templates.TemplateResponse(request, "not_found.html", {"message": str(exc)}, status_code=404)

    app.add_exception_handler(ProfileNotFoundError, not_found)
    app.add_exception_handler(RunNotFoundError, not_found)

    return app
