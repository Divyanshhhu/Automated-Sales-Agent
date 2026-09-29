import copy
import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import db
from src.profiles import Profile, create_profile
from src.web import app as app_module
from src.web import runner
from src.web.app import create_app

BASE_URL = "http://127.0.0.1"

VALID_CONFIG: dict = {
    "product": {
        "name": "Test Product",
        "description": "A test product.",
        "problem_solved": "Slow lead response.",
        "differentiators": ["Fast", "Always on"],
    },
    "icp": {
        "industry_keywords": ["real estate developer", "realty"],
        "employee_range": [51, 2000],
        "geographies": ["Mumbai", "Pune"],
        "min_icp_fit_score": 50,
    },
    "signal_taxonomy": {
        "expansion_launch": "{company} new project launch",
        "direct_pain_point": "{company} customer complaint",
    },
}


@pytest.fixture
def valid_config() -> dict:
    return copy.deepcopy(VALID_CONFIG)


@pytest.fixture(autouse=True)
def _never_touch_the_real_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets its own throwaway SQLite file -- including tests that
    don't ask for one, since API clients now record usage to the database.
    """
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")


@pytest.fixture
def temp_db() -> Path:
    """The test's throwaway database file (already in place for every test)."""
    return db.DB_PATH


@pytest.fixture
def profile(temp_db: Path) -> Profile:
    return create_profile("Test", copy.deepcopy(VALID_CONFIG))


def insert_company(company_id: str = "c1", name: str = "Acme Realty", employee_count: int = 200,
                   city: str = "Mumbai", description: str = "A realty firm") -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO companies (id, name, domain, employee_count, industry, city, state, country,
                               short_description, raw_json, discovered_at)
        VALUES (?, ?, ?, ?, NULL, ?, NULL, 'India', ?, '{}', '2026-01-01')
        """,
        (company_id, name, f"{company_id}.in", employee_count, city, description),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def company_row(profile: Profile) -> dict:
    """A company scored under `profile`, shaped like the pipeline's qualifying rows."""
    insert_company()
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)
        VALUES (?, 'c1', 100.0, 'High', '{}', '2026-01-01')
        """,
        (profile.id,),
    )
    conn.commit()
    row = dict(
        conn.execute(
            """
            SELECT c.*, s.score AS icp_fit_score, s.label AS icp_fit_label
            FROM companies c JOIN company_scores s ON s.company_id = c.id WHERE c.id = 'c1'
            """
        ).fetchone()
    )
    conn.close()
    return row


def insert_evidence(profile_id: int, company_id: str, *, category: str = "expansion_launch",
                    fact: str = "Launched a new tower", url: str = "https://news.example/1") -> int:
    conn = db.get_connection()
    cursor = conn.execute(
        """
        INSERT INTO evidence_items (profile_id, company_id, category, fact_text, source_url, retrieved_at)
        VALUES (?, ?, ?, ?, ?, '2026-01-01')
        """,
        (profile_id, company_id, category, fact, url),
    )
    conn.commit()
    conn.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


def insert_memo(profile_id: int, company_id: str, *, why: str = "Launch news [INFERENCE].",
                use_case: str = "Fit [INFERENCE].", issues: list[str] | None = None,
                status: str = "pending", score: float = 100.0, confidence: str = "\U0001F7E1 Plausible fit",
                evidence_ids: list[int] | None = None) -> int:
    conn = db.get_connection()
    cursor = conn.execute(
        """
        INSERT INTO memos (profile_id, company_id, icp_fit_score, icp_fit_label, signal_confidence,
                           why_relevant_text, potential_use_case_text, evidence_ids_json,
                           citation_issues, review_status, generated_at)
        VALUES (?, ?, ?, 'High', ?, ?, ?, ?, ?, ?, '2026-01-01')
        """,
        (
            profile_id, company_id, score, confidence, why, use_case,
            json.dumps(evidence_ids) if evidence_ids is not None else None,
            json.dumps(issues) if issues else None, status,
        ),
    )
    conn.commit()
    conn.close()
    assert cursor.lastrowid is not None
    return cursor.lastrowid


@pytest.fixture
def starter_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The web UI's "new profile" starter file, pointed at VALID_CONFIG."""
    path = tmp_path / "starter.json"
    path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    monkeypatch.setattr(app_module, "DEFAULT_PROFILE_PATH", path)
    return path


@pytest.fixture
def client(temp_db: Path, starter_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), base_url=BASE_URL) as test_client:
        yield test_client
    runner.wait_for_active_run(timeout=5)
