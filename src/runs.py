"""Pipeline run records: which profile ran, with exactly which config, when,
and how it ended.

The config is snapshotted at start, so editing a profile later never changes
what an earlier run's memos were based on. Status + stats are what the UI
will poll to show progress.
"""
import json
from dataclasses import dataclass

from .db import get_connection, utc_now
from .profiles import Profile

MAX_ERROR_LENGTH = 2000


@dataclass(frozen=True)
class Run:
    id: int
    profile_id: int
    config: dict
    discover_limit: int
    status: str
    stats: dict
    error: str | None
    started_at: str
    finished_at: str | None


def start_run(profile: Profile, discover_limit: int) -> int:
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO runs (profile_id, config_snapshot_json, discover_limit, status, started_at)
            VALUES (?, ?, ?, 'running', ?)
            """,
            (profile.id, json.dumps(profile.config), discover_limit, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _finish(run_id: int, status: str, stats: dict, error: str | None) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE runs SET status=?, stats_json=?, error=?, finished_at=? WHERE id=?",
            (status, json.dumps(stats), error, utc_now(), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def complete_run(run_id: int, stats: dict) -> None:
    _finish(run_id, "succeeded", stats, None)


def fail_run(run_id: int, stats: dict, error: str) -> None:
    _finish(run_id, "failed", stats, error[:MAX_ERROR_LENGTH])


def get_run(run_id: int) -> Run:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise LookupError(f"No run with id {run_id}")
    return Run(
        id=row["id"],
        profile_id=row["profile_id"],
        config=json.loads(row["config_snapshot_json"]),
        discover_limit=row["discover_limit"],
        status=row["status"],
        stats=json.loads(row["stats_json"]) if row["stats_json"] else {},
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )
