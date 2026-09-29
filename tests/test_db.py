import json
import sqlite3
from pathlib import Path

import pytest

from src import db
from src.config import ConfigError
from src.profiles import Profile
from tests.conftest import VALID_CONFIG, insert_company

# The exact pre-migration Phase 1 schema, as databases created before
# versioned migrations (user_version 0) have it.
LEGACY_SCHEMA = """
CREATE TABLE companies (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT, employee_count INTEGER, industry TEXT,
    city TEXT, state TEXT, country TEXT, short_description TEXT, raw_json TEXT,
    icp_fit_score REAL, icp_fit_label TEXT, discovered_at TEXT
);
CREATE TABLE evidence_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, category TEXT NOT NULL,
    fact_text TEXT NOT NULL, source_url TEXT, source_date TEXT, retrieved_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies (id)
);
CREATE UNIQUE INDEX idx_evidence_unique ON evidence_items (company_id, category, source_url);
CREATE TABLE memos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, company_id TEXT NOT NULL, icp_fit_score REAL,
    icp_fit_label TEXT, signal_confidence TEXT, why_relevant_text TEXT, potential_use_case_text TEXT,
    evidence_ids_json TEXT, citation_issues TEXT, review_status TEXT DEFAULT 'pending', generated_at TEXT,
    FOREIGN KEY (company_id) REFERENCES companies (id)
);
INSERT INTO companies VALUES ('acme.in', 'Acme', 'acme.in', 200, NULL, 'Pune', NULL, 'India', 'realty',
                              '{}', 100.0, 'High', '2026-01-01');
INSERT INTO companies VALUES ('unscored.in', 'Unscored', 'unscored.in', 10, NULL, NULL, NULL, NULL, NULL,
                              '{}', NULL, NULL, '2026-01-01');
INSERT INTO evidence_items (id, company_id, category, fact_text, source_url)
    VALUES (17, 'acme.in', 'expansion_launch', 'New tower', 'https://news/1');
INSERT INTO memos (id, company_id, icp_fit_score, icp_fit_label, signal_confidence, why_relevant_text,
                   review_status)
    VALUES (5, 'acme.in', 100.0, 'High', 'Plausible', 'New tower [E17].', 'pending');
"""


@pytest.fixture
def legacy_db(temp_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    conn = sqlite3.connect(temp_db)
    conn.executescript(LEGACY_SCHEMA)
    conn.close()
    profile_path = tmp_path / "product_profile.json"
    profile_path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    monkeypatch.setattr(db, "DEFAULT_PROFILE_PATH", profile_path)
    return temp_db


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_is_fully_migrated(temp_db: Path) -> None:
    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    expected_tables = {"profiles", "runs", "companies", "company_scores", "evidence_items", "memos"}
    expected_tables |= {"contacts", "outreach_drafts"}
    assert expected_tables <= _tables(conn)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # no legacy data -> no Default profile invented
    assert conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0
    conn.close()


def test_reopening_is_a_no_op(temp_db: Path) -> None:
    db.get_connection().close()
    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)
    conn.close()


def test_foreign_keys_are_enforced(profile: Profile) -> None:
    conn = db.get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memos (profile_id, company_id) VALUES (?, 'no-such-company')",
            (profile.id,),
        )
    conn.close()


def test_review_status_is_constrained(company_row: dict, profile: Profile) -> None:
    conn = db.get_connection()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO memos (profile_id, company_id, review_status) VALUES (?, 'c1', 'maybe')",
            (profile.id,),
        )
    conn.close()


def test_one_memo_per_profile_and_company(company_row: dict, profile: Profile) -> None:
    conn = db.get_connection()
    insert = "INSERT INTO memos (profile_id, company_id) VALUES (?, 'c1')"
    conn.execute(insert, (profile.id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(insert, (profile.id,))
    conn.close()


def test_legacy_database_is_adopted_into_default_profile(legacy_db: Path) -> None:
    conn = db.get_connection()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS)

    profile = conn.execute("SELECT * FROM profiles").fetchone()
    assert profile["name"] == db.DEFAULT_PROFILE_NAME
    assert json.loads(profile["config_json"]) == VALID_CONFIG

    columns = {r["name"] for r in conn.execute("PRAGMA table_info(companies)")}
    assert "icp_fit_score" not in columns
    assert conn.execute("SELECT COUNT(*) FROM companies").fetchone()[0] == 2

    scores = conn.execute("SELECT * FROM company_scores").fetchall()
    assert [(s["profile_id"], s["company_id"], s["score"], s["label"]) for s in scores] == [
        (profile["id"], "acme.in", 100.0, "High")
    ]

    evidence = conn.execute("SELECT * FROM evidence_items").fetchone()
    assert evidence["id"] == 17  # memo text cites [E17]: ids must survive the migration
    assert evidence["profile_id"] == profile["id"]

    memo = conn.execute("SELECT * FROM memos").fetchone()
    assert (memo["id"], memo["profile_id"], memo["run_id"]) == (5, profile["id"], None)
    assert memo["why_relevant_text"] == "New tower [E17]."
    assert memo["review_status"] == "pending"
    conn.close()


def test_existing_evidence_counts_as_searched_after_upgrade(legacy_db: Path) -> None:
    conn = db.get_connection()
    rows = conn.execute("SELECT category, results_kept FROM evidence_searches").fetchall()
    conn.close()
    # the legacy company had expansion evidence, so only that search is marked done
    assert [(r["category"], r["results_kept"]) for r in rows] == [("expansion_launch", 1)]


def test_failed_migration_rolls_back_and_keeps_legacy_data(
    legacy_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(db, "DEFAULT_PROFILE_PATH", tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        db.get_connection()

    conn = sqlite3.connect(legacy_db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1  # migration 1 committed, 2 rolled back
    assert "profiles" not in _tables(conn)
    assert conn.execute("SELECT icp_fit_score FROM companies WHERE id='acme.in'").fetchone()[0] == 100.0
    assert conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0] == 1
    conn.close()


def test_migration_can_be_retried_after_fixing_the_cause(
    legacy_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good_path = db.DEFAULT_PROFILE_PATH
    monkeypatch.setattr(db, "DEFAULT_PROFILE_PATH", tmp_path / "missing.json")
    with pytest.raises(ConfigError):
        db.get_connection()

    monkeypatch.setattr(db, "DEFAULT_PROFILE_PATH", good_path)
    conn = db.get_connection()
    assert conn.execute("SELECT COUNT(*) FROM memos").fetchone()[0] == 1
    conn.close()


def test_company_insert_helper_matches_schema(temp_db: Path) -> None:
    insert_company("x1")
    conn = db.get_connection()
    assert conn.execute("SELECT name FROM companies WHERE id='x1'").fetchone()[0] == "Acme Realty"
    conn.close()
