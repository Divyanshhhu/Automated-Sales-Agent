import copy
from pathlib import Path

import pytest

from src import db

VALID_CONFIG = {
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
def company_row(temp_db: Path) -> dict:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO companies (id, name, domain, employee_count, industry, city, state, country,
                               short_description, icp_fit_score, icp_fit_label)
        VALUES ('c1', 'Acme Realty', 'acme.in', 200, 'real estate', 'Mumbai', 'Maharashtra', 'India',
                'A realty firm', 100.0, 'High')
        """
    )
    conn.commit()
    row = dict(conn.execute("SELECT * FROM companies WHERE id='c1'").fetchone())
    conn.close()
    return row
