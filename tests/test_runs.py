import pytest

from src.profiles import Profile
from src.runs import (
    RunNotFoundError,
    complete_run,
    create_run,
    fail_orphaned_runs,
    fail_run,
    get_run,
    list_runs,
    update_stats,
)


def test_create_queued_run_snapshots_config(profile: Profile) -> None:
    run = get_run(create_run(profile, 10, status="queued"))
    assert (run.status, run.discover_limit, run.profile_name) == ("queued", 10, "Test")
    assert run.config == profile.config
    assert run.is_active


def test_new_run_cannot_start_finished(profile: Profile) -> None:
    with pytest.raises(ValueError):
        create_run(profile, 10, status="succeeded")


def test_progress_then_completion(profile: Profile) -> None:
    run_id = create_run(profile, 10)
    update_stats(run_id, {"stage": "evidence", "evidence_done": 2})
    assert get_run(run_id).stats == {"stage": "evidence", "evidence_done": 2}

    complete_run(run_id, {"stage": "done"})
    run = get_run(run_id)
    assert run.status == "succeeded"
    assert run.finished_at is not None
    assert not run.is_active


def test_fail_run_truncates_huge_errors(profile: Profile) -> None:
    run_id = create_run(profile, 10)
    fail_run(run_id, {}, "x" * 10_000)
    run = get_run(run_id)
    assert run.status == "failed"
    assert run.error is not None and len(run.error) == 2000


def test_fail_orphaned_runs_only_touches_active_runs(profile: Profile) -> None:
    queued = create_run(profile, 10, status="queued")
    running = create_run(profile, 10)
    done = create_run(profile, 10)
    complete_run(done, {})

    assert fail_orphaned_runs() == 2
    assert get_run(queued).status == "failed"
    assert get_run(running).status == "failed"
    assert get_run(done).status == "succeeded"


def test_list_runs_newest_first(profile: Profile) -> None:
    ids = [create_run(profile, 10) for _ in range(3)]
    assert [r.id for r in list_runs()] == ids[::-1]
    assert len(list_runs(limit=2)) == 2


def test_missing_run(profile: Profile) -> None:
    with pytest.raises(RunNotFoundError):
        get_run(999)
