import pytest

from src.icp_scorer import _employee_score, score_company

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
    assert score_company(_row(), ICP) == (100.0, "High")


def test_wrong_location_drops_to_medium() -> None:
    score, label = score_company(_row(city="Chennai", state="Tamil Nadu"), ICP)
    assert score == 60.0
    assert label == "Medium"


def test_nothing_matches_is_low() -> None:
    row = _row(
        employee_count=10_000, city="Austin", state="Texas", country="USA",
        industry="software", short_description="SaaS",
    )
    assert score_company(row, ICP) == (0.0, "Low")


def test_unknown_fields_get_partial_credit() -> None:
    row = _row(employee_count=None, industry=None, short_description=None)
    # 0.5 * 30 employee + 40 location + 0.3 * 30 industry
    assert score_company(row, ICP) == (64.0, "Medium")


@pytest.mark.parametrize(
    ("count", "expected"),
    [(51, 1.0), (2000, 1.0), (40, 0.5), (2900, 0.5), (3100, 0.0), (None, 0.5)],
)
def test_employee_score_bounds(count: int | None, expected: float) -> None:
    assert _employee_score(count, 51, 2000) == expected
