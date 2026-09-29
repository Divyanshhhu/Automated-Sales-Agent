import copy
from pathlib import Path

import pytest

from src import db
from src.profiles import Profile, create_profile

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


@pytest.fixture
def temp_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Points every get_connection() call at a fresh throwaway SQLite file."""
    path = tmp_path / "test.sqlite"
    monkeypatch.setattr(db, "DB_PATH", path)
    return path


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
