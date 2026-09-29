"""Pipeline run records: which profile ran, with exactly which config, when,
and how it ended.

The config is snapshotted at creation, so editing a profile later never
changes what an earlier run's memos were based on. Status + stats are
updated as the run progresses, which is what the UI polls to show progress.
"""
import json
import sqlite3
from dataclasses import dataclass

from .db import get_connection, utc_now
from .profiles import Profile

MAX_ERROR_LENGTH = 2000
ACTIVE_STATUSES = ("queued", "running")


class RunNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Run:
    id: int
    profile_id: int
    profile_name: str
    config: dict
    discover_limit: int
    status: str
    stats: dict
    error: str | None
    started_at: str
    finished_at: str | None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES


def create_run(profile: Profile, discover_limit: int, *, status: str = "running") -> int:
    if status not in ACTIVE_STATUSES:
        raise ValueError(f"A new run must start as one of {ACTIVE_STATUSES}, not {status!r}")
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO runs (profile_id, config_snapshot_json, discover_limit, status, started_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (profile.id, json.dumps(profile.config), discover_limit, status, utc_now()),
        )
        conn.commit()
    finally:
        conn.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def _update(run_id: int, sql_set: str, params: tuple) -> None:
    conn = get_connection()
    try:
        conn.execute(f"UPDATE runs SET {sql_set} WHERE id=?", (*params, run_id))
        conn.commit()
    finally:
        conn.close()


def mark_running(run_id: int) -> None:
    _update(run_id, "status='running'", ())


def update_stats(run_id: int, stats: dict) -> None:
    """Progress checkpoint while the run is still going."""
    _update(run_id, "stats_json=?", (json.dumps(stats),))


def complete_run(run_id: int, stats: dict) -> None:
    _update(
        run_id,
        "status='succeeded', stats_json=?, error=NULL, finished_at=?",
        (json.dumps(stats), utc_now()),
    )


def fail_run(run_id: int, stats: dict, error: str) -> None:
    _update(
        run_id,
        "status='failed', stats_json=?, error=?, finished_at=?",
        (json.dumps(stats), error[:MAX_ERROR_LENGTH], utc_now()),
    )


def fail_orphaned_runs() -> int:
    """Marks runs still queued/running as failed. Called when the web server
    starts: a run's thread dies with the server process, so anything still
    "active" at startup was interrupted and would otherwise look stuck
    forever. Returns the number of runs marked.
    """
    conn = get_connection()
    try:
        cursor = conn.execute(
            f"""
            UPDATE runs SET status='failed', error=?, finished_at=?
            WHERE status IN ({",".join("?" * len(ACTIVE_STATUSES))})
            """,
            ("Interrupted: the server stopped before this run finished", utc_now(), *ACTIVE_STATUSES),
        )
        conn.commit()
    finally:
        conn.close()
    return cursor.rowcount


_SELECT_RUNS = """
    SELECT r.*, p.name AS profile_name FROM runs r JOIN profiles p ON p.id = r.profile_id
"""


def _from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"],
        profile_id=row["profile_id"],
        profile_name=row["profile_name"],
        config=json.loads(row["config_snapshot_json"]),
        discover_limit=row["discover_limit"],
        status=row["status"],
        stats=json.loads(row["stats_json"]) if row["stats_json"] else {},
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
    )


def get_run(run_id: int) -> Run:
    conn = get_connection()
    try:
        row = conn.execute(f"{_SELECT_RUNS} WHERE r.id=?", (run_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise RunNotFoundError(f"No run with id {run_id}")
    return _from_row(row)


def list_runs(limit: int = 50) -> list[Run]:
    conn = get_connection()
    try:
        rows = conn.execute(f"{_SELECT_RUNS} ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    return [_from_row(r) for r in rows]
