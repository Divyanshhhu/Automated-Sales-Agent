from datetime import datetime, timezone
from pathlib import Path

import pytest

from src import usage
from src.db import get_connection
from src.profiles import Profile
from src.runs import create_run
from src.usage import (
    estimate_run,
    month_cost,
    record_exa,
    record_openai,
    record_tavily,
    run_cost,
    usage_context,
)


def test_records_are_tagged_with_the_current_context(profile: Profile) -> None:
    run_id = create_run(profile, 5)
    with usage_context("find_leads", profile_id=profile.id, run_id=run_id):
        record_tavily(1, "q")
        record_exa(0.022, "company: q")
        record_openai(3000, 400, "research_memo")
    record_tavily(2)  # outside any context

    cost = run_cost(run_id)
    assert cost.calls == 3
    assert cost.tavily_credits == 1
    assert cost.openai_tokens == 3400
    assert cost.by_provider == {"tavily": 0.008, "exa": 0.022, "openai": 0.0005}
    assert cost.total_usd == pytest.approx(0.0305)

    conn = get_connection()
    untagged = conn.execute("SELECT action, run_id FROM api_usage WHERE run_id IS NULL").fetchone()
    conn.close()
    assert (untagged["action"], untagged["run_id"]) == ("other", None)


def test_exa_without_a_reported_cost_uses_the_fallback(temp_db: Path) -> None:
    record_exa(None)
    assert month_cost().by_provider["exa"] == usage.EXA_FALLBACK_PER_SEARCH


def test_month_cost_only_counts_this_month(temp_db: Path) -> None:
    record_tavily(3)
    assert month_cost().tavily_credits == 3
    assert month_cost(datetime(2099, 1, 15, tzinfo=timezone.utc)).calls == 0


def test_recording_failure_never_breaks_the_call(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> None:
        raise RuntimeError("disk full")

    monkeypatch.setattr(usage, "get_connection", broken)
    record_tavily(1)  # logged, not raised


def test_estimate() -> None:
    estimate = estimate_run(25)
    assert estimate.tavily_credits == 125
    assert estimate.tavily_usd == 1.0
    assert estimate.exa_usd == 0.04
    assert estimate.total_usd == 1.06
    assert estimate.free_share_percent == 12
    assert estimate_run(0).tavily_credits == 5  # at least one company
