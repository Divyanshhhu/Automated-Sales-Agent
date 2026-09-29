import copy
from pathlib import Path

import pytest

from src import discovery, evidence, export, memo_generator
from src.db import get_connection
from src.pipeline import run_pipeline
from src.profiles import Profile, create_profile
from src.runs import get_run
from tests.conftest import VALID_CONFIG


def _exa_result(domain: str, employees: int, city: str) -> dict:
    return {
        "url": f"https://{domain}",
        "title": "Real estate developer",
        "content": "",
        "entity": {
            "name": domain.split(".")[0].title(),
            "description": "Residential realty developer",
            "workforce": {"total": employees},
            "headquarters": {"city": city, "country": "India"},
        },
    }


@pytest.fixture
def fake_apis(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    """Fakes Exa, Tavily and OpenAI; records calls so tests can assert on them."""
    calls: dict = {"tavily": 0, "llm": []}

    def fake_exa(query: str, **_: object) -> list[dict]:
        return [_exa_result("small.in", 100, "Mumbai"), _exa_result("large.in", 5000, "Mumbai")]

    def fake_tavily(query: str, **_: object) -> list[dict]:
        calls["tavily"] += 1
        return [{"content": f"Buyers complain of slow response ({query})", "url": f"https://src/{query}"}]

    def fake_llm(product: dict, company_row, evidence_items: list[dict]) -> dict:
        calls["llm"].append(company_row["id"])
        first = evidence_items[0]["id"]
        return {"why_relevant": f"Launch news [E{first}].", "potential_use_case": "Fit [INFERENCE]."}

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    monkeypatch.setattr(evidence, "tavily_search", fake_tavily)
    monkeypatch.setattr(memo_generator, "_call_llm", fake_llm)
    monkeypatch.setattr(export, "OUTPUT_DIR", tmp_path / "output")
    return calls


def _memos(profile_id: int) -> dict[str, dict]:
    conn = get_connection()
    rows = conn.execute("SELECT * FROM memos WHERE profile_id=?", (profile_id,)).fetchall()
    conn.close()
    return {r["company_id"]: dict(r) for r in rows}


def _profile_with_range(name: str, employee_range: list[int]) -> Profile:
    config = copy.deepcopy(VALID_CONFIG)
    config["icp"]["employee_range"] = employee_range
    # 80, so a company that only misses on size (70 = location + industry) doesn't qualify
    config["icp"]["min_icp_fit_score"] = 80
    return create_profile(name, config)


def test_successful_run_is_recorded(temp_db: Path, fake_apis: dict) -> None:
    profile = _profile_with_range("Mid-size", [51, 2000])
    run_id = run_pipeline(profile, discover_limit=5)

    run = get_run(run_id)
    assert run.status == "succeeded"
    assert run.finished_at is not None
    assert run.config == profile.config
    assert run.stats["discovered"] == 2
    assert run.stats["qualifying"] == 1  # large.in (5000 employees) scores 70 < 80
    assert run.stats["memos_generated"] == 1
    assert Path(run.stats["csv_path"]).name.startswith("memos_mid-size_")

    memos = _memos(profile.id)
    assert set(memos) == {"small.in"}
    assert memos["small.in"]["run_id"] == run_id


def test_profiles_do_not_overwrite_each_other(temp_db: Path, fake_apis: dict) -> None:
    mid = _profile_with_range("Mid-size", [51, 2000])
    large = _profile_with_range("Large", [4000, 6000])
    run_pipeline(mid, discover_limit=5)
    run_pipeline(large, discover_limit=5)

    assert set(_memos(mid.id)) == {"small.in"}
    assert set(_memos(large.id)) == {"large.in"}

    conn = get_connection()
    scores = {
        (r["profile_id"], r["company_id"]): r["score"]
        for r in conn.execute("SELECT * FROM company_scores")
    }
    conn.close()
    # same company, two different scores -- one per profile
    assert scores[(mid.id, "small.in")] == 100.0
    assert scores[(large.id, "small.in")] == 70.0


def test_rerun_skips_companies_that_already_have_memos(temp_db: Path, fake_apis: dict) -> None:
    profile = _profile_with_range("Mid-size", [51, 2000])
    run_pipeline(profile, discover_limit=5)
    tavily_calls, llm_calls = fake_apis["tavily"], len(fake_apis["llm"])

    second = get_run(run_pipeline(profile, discover_limit=5))
    assert second.stats["qualifying"] == 0
    assert fake_apis["tavily"] == tavily_calls
    assert len(fake_apis["llm"]) == llm_calls


def test_memo_failure_is_counted_and_rerun_retries_it(
    temp_db: Path, fake_apis: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _profile_with_range("Mid-size", [51, 2000])

    def broken_llm(*_: object) -> dict:
        raise RuntimeError("model unavailable")

    working_llm = memo_generator._call_llm
    monkeypatch.setattr(memo_generator, "_call_llm", broken_llm)
    first = get_run(run_pipeline(profile, discover_limit=5))
    assert first.status == "succeeded"  # one company failing doesn't fail the batch
    assert first.stats["memos_failed"] == 1
    assert _memos(profile.id) == {}

    monkeypatch.setattr(memo_generator, "_call_llm", working_llm)
    tavily_calls = fake_apis["tavily"]
    second = get_run(run_pipeline(profile, discover_limit=5))
    assert second.stats["memos_generated"] == 1
    assert second.stats["evidence_added"] == 0  # evidence from the first run was reused
    assert fake_apis["tavily"] == tavily_calls


@pytest.mark.parametrize("error", [RuntimeError("exa down"), KeyboardInterrupt()])
def test_crashed_run_is_marked_failed(
    temp_db: Path, fake_apis: dict, monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    profile = _profile_with_range("Mid-size", [51, 2000])

    def crash(*_: object, **__: object) -> list:
        raise error

    monkeypatch.setattr(discovery, "exa_search", crash)
    with pytest.raises(type(error)):
        run_pipeline(profile, discover_limit=5)

    conn = get_connection()
    row = conn.execute("SELECT * FROM runs WHERE profile_id=?", (profile.id,)).fetchone()
    conn.close()
    run = get_run(row["id"])
    assert run.status == "failed"
    assert run.error is not None and type(error).__name__ in run.error
    assert run.finished_at is not None


def test_run_keeps_config_snapshot_after_profile_edit(temp_db: Path, fake_apis: dict) -> None:
    from src.profiles import update_profile

    profile = _profile_with_range("Mid-size", [51, 2000])
    run_id = run_pipeline(profile, discover_limit=5)
    changed = copy.deepcopy(profile.config)
    changed["icp"]["employee_range"] = [10, 20]
    update_profile(profile.id, config=changed)

    assert get_run(run_id).config["icp"]["employee_range"] == [51, 2000]
