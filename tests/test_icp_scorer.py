import json
from pathlib import Path

import pytest

from src.db import get_connection
from src.icp_scorer import IcpScore, _employee_score, score_companies_for_profile, score_company
from src.profiles import Profile
from tests.conftest import insert_company

ICP = {
    "industry_keywords": ["real estate developer", "realty"],
    "employee_range": [51, 2000],
    "geographies": ["Mumbai", "Pune"],
}


def _row(**overrides: object) -> dict:
    row = {
        "employee_count": 200,
        "city": "Mumbai",
        "state": "Maharashtra",
        "country": "India",
        "industry": "real estate",
        "short_description": "Leading realty firm",
    }
    row.update(overrides)
    return row


def test_perfect_fit_is_high() -> None:
    assert score_company(_row(), ICP) == IcpScore(
        100.0, "High", {"employee": 30.0, "location": 40.0, "industry": 30.0}
    )


def test_wrong_location_drops_to_medium() -> None:
    result = score_company(_row(city="Chennai", state="Tamil Nadu"), ICP)
    assert (result.score, result.label) == (60.0, "Medium")
    assert result.breakdown["location"] == 0.0


def test_nothing_matches_is_low() -> None:
    row = _row(
        employee_count=10_000, city="Austin", state="Texas", country="USA",
        industry="software", short_description="SaaS",
    )
    assert score_company(row, ICP) == IcpScore(
        0.0, "Low", {"employee": 0.0, "location": 0.0, "industry": 0.0}
    )


def test_unknown_fields_get_partial_credit() -> None:
    row = _row(employee_count=None, industry=None, short_description=None)
    # 0.5 * 30 employee + 40 location + 0.3 * 30 industry
    assert score_company(row, ICP) == IcpScore(
        64.0, "Medium", {"employee": 15.0, "location": 40.0, "industry": 9.0}
    )


@pytest.mark.parametrize(
    ("count", "expected"),
    [(51, 1.0), (2000, 1.0), (40, 0.5), (2900, 0.5), (3100, 0.0), (None, 0.5)],
)
def test_employee_score_bounds(count: int | None, expected: float) -> None:
    assert _employee_score(count, 51, 2000) == expected


def _stored_scores(profile_id: int) -> dict[str, tuple[float, dict]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT company_id, score, breakdown_json FROM company_scores WHERE profile_id=?", (profile_id,)
    ).fetchall()
    conn.close()
    return {r["company_id"]: (r["score"], json.loads(r["breakdown_json"])) for r in rows}


def test_scores_are_stored_with_breakdown(profile: Profile) -> None:
    insert_company("c1")
    assert score_companies_for_profile(profile.id, profile.config["icp"], ["c1"]) == 1
    score, breakdown = _stored_scores(profile.id)["c1"]
    assert score == 100.0
    assert breakdown == {"employee": 30.0, "location": 40.0, "industry": 30.0}


def test_rescoring_includes_previously_scored_companies(profile: Profile) -> None:
    insert_company("c1", employee_count=200)
    insert_company("c2", employee_count=300)
    icp = profile.config["icp"]
    score_companies_for_profile(profile.id, icp, ["c1"])

    # profile edited: now only large companies fit; c1 must be re-evaluated too
    narrowed = {**icp, "employee_range": [3000, 3500]}
    assert score_companies_for_profile(profile.id, narrowed, ["c2"]) == 2
    scores = _stored_scores(profile.id)
    assert scores["c1"][1]["employee"] == 0.0
    assert scores["c2"][1]["employee"] == 0.0


def test_unknown_company_ids_are_skipped(profile: Profile, temp_db: Path) -> None:
    assert score_companies_for_profile(profile.id, profile.config["icp"], ["missing"]) == 0
    assert _stored_scores(profile.id) == {}
