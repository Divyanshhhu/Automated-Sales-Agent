"""What the paid APIs actually cost.

Every paid call records one api_usage row: Exa with the cost it reports,
Tavily as credits, OpenAI as tokens -- priced with the rates below. The
action and campaign it was for come from a context set by whoever started
the work (a run, a button in the UI), so the API clients don't need to know.

Recording never breaks the call it describes: a failure to record is logged
and the work carries on.
"""

import contextvars
import logging
import math
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone

from .db import get_connection, utc_now

logger = logging.getLogger("sales_agent")


def _price(env_name: str, default: float) -> float:
    try:
        return float(os.environ.get(env_name, default))
    except ValueError:
        logger.warning("Ignoring invalid %s; using %s", env_name, default)
        return default


# Prices in USD, Sept 2026. Override in .env if your plan differs.
TAVILY_PRICE_PER_CREDIT = _price("TAVILY_PRICE_PER_CREDIT", 0.008)  # pay-as-you-go
TAVILY_FREE_CREDITS_PER_MONTH = int(_price("TAVILY_FREE_CREDITS_PER_MONTH", 1000))
OPENAI_PRICE_PER_M_INPUT = _price("OPENAI_PRICE_PER_M_INPUT", 0.10)  # gpt-6-luna
OPENAI_PRICE_PER_M_OUTPUT = _price("OPENAI_PRICE_PER_M_OUTPUT", 0.50)
EXA_FALLBACK_PER_SEARCH = 0.022  # only if Exa's response doesn't report its cost

# For estimates only (live averages): what one company costs to research and write up.
TAVILY_CREDITS_PER_COMPANY = 5
OPENAI_COST_PER_MEMO = 0.0005
COMPANIES_PER_EXA_SEARCH = 20


@dataclass(frozen=True)
class _Context:
    action: str
    profile_id: int | None
    run_id: int | None


_current: contextvars.ContextVar[_Context | None] = contextvars.ContextVar("usage_context", default=None)


@contextmanager
def usage_context(action: str, *, profile_id: int | None = None, run_id: int | None = None) -> Iterator[None]:
    """Tags every API call made inside the block with this action/campaign/run."""
    token = _current.set(_Context(action, profile_id, run_id))
    try:
        yield
    finally:
        _current.reset(token)


def record(provider: str, units: float, unit: str, cost_usd: float, detail: str = "") -> None:
    context = _current.get() or _Context("other", None, None)
    try:
        conn = get_connection()
        try:
            conn.execute(
                """
                INSERT INTO api_usage
                    (created_at, provider, units, unit, cost_usd, action, profile_id, run_id, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    provider,
                    units,
                    unit,
                    cost_usd,
                    context.action,
                    context.profile_id,
                    context.run_id,
                    detail[:200],
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        logger.exception("Could not record %s usage (the API call itself succeeded)", provider)


def record_tavily(credits: int, detail: str = "") -> None:
    record("tavily", credits, "credits", credits * TAVILY_PRICE_PER_CREDIT, detail)


def record_openai(input_tokens: int, output_tokens: int, detail: str = "") -> None:
    cost = (input_tokens * OPENAI_PRICE_PER_M_INPUT + output_tokens * OPENAI_PRICE_PER_M_OUTPUT) / 1_000_000
    record("openai", input_tokens + output_tokens, "tokens", cost, detail)


def record_exa(reported_cost: object, detail: str = "") -> None:
    cost = reported_cost if isinstance(reported_cost, (int, float)) else EXA_FALLBACK_PER_SEARCH
    record("exa", 1, "searches", float(cost), detail)


# ---------- reading ----------


@dataclass(frozen=True)
class CostSummary:
    total_usd: float
    by_provider: dict[str, float]
    tavily_credits: int
    openai_tokens: int
    calls: int


def _summarize(where: str, params: tuple) -> CostSummary:
    conn = get_connection()
    try:
        rows = conn.execute(
            f"""
            SELECT provider, SUM(cost_usd) AS cost, SUM(units) AS units, COUNT(*) AS calls
            FROM api_usage WHERE {where} GROUP BY provider
            """,
            params,
        ).fetchall()
    finally:
        conn.close()
    by_provider = {r["provider"]: round(r["cost"] or 0.0, 4) for r in rows}
    units = {r["provider"]: r["units"] or 0 for r in rows}
    return CostSummary(
        total_usd=round(sum(by_provider.values()), 4),
        by_provider=by_provider,
        tavily_credits=int(units.get("tavily", 0)),
        openai_tokens=int(units.get("openai", 0)),
        calls=sum(r["calls"] for r in rows),
    )


def run_cost(run_id: int) -> CostSummary:
    return _summarize("run_id = ?", (run_id,))


def month_cost(now: datetime | None = None) -> CostSummary:
    """Everything recorded since the start of this calendar month (UTC) --
    Tavily's free credits reset monthly, so this is the number that matters.
    """
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    return _summarize("created_at >= ?", (start,))


@dataclass(frozen=True)
class RunEstimate:
    exa_usd: float
    tavily_usd: float
    tavily_credits: int
    openai_usd: float
    total_usd: float
    free_share_percent: int  # of the monthly free Tavily credits


def estimate_run(new_companies: int) -> RunEstimate:
    """Upper bound: assumes every new company qualifies and is researched."""
    n = max(1, new_companies)
    credits = n * TAVILY_CREDITS_PER_COMPANY
    exa = math.ceil(n / COMPANIES_PER_EXA_SEARCH) * EXA_FALLBACK_PER_SEARCH
    tavily = credits * TAVILY_PRICE_PER_CREDIT
    openai = n * OPENAI_COST_PER_MEMO
    return RunEstimate(
        exa_usd=round(exa, 2),
        tavily_usd=round(tavily, 2),
        tavily_credits=credits,
        openai_usd=round(openai, 2),
        total_usd=round(exa + tavily + openai, 2),
        free_share_percent=round(100 * credits / TAVILY_FREE_CREDITS_PER_MONTH)
        if TAVILY_FREE_CREDITS_PER_MONTH
        else 0,
    )
