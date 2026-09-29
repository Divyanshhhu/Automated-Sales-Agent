import json
from pathlib import Path

import pytest
import requests

from src import discovery
from src.db import get_connection
from src.discovery import (
    _domain_from_url,
    _is_blocked,
    company_from_result,
    discover_companies,
    store_companies,
)

ICP = {"industry_keywords": ["real estate developer"], "geographies": ["Mumbai", "Pune"]}


def _entity(name: str = "Acme Realty", employees: int | None = 200, city: str | None = "Pune") -> dict:
    return {
        "name": name,
        "description": "Residential real estate developer",
        "workforce": {"total": employees},
        "headquarters": {"address": "Pune, Maharashtra", "city": city, "country": "India"},
    }


def _result(
    url: str,
    title: str = "Real estate developer in Mumbai",
    content: str = "",
    entity: dict | None = None,
) -> dict:
    return {"url": url, "title": title, "content": content, "entity": entity or _entity(name=url)}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.lodhagroup.com/projects", "lodhagroup.com"),
        ("https://godrejproperties.com", "godrejproperties.com"),
        ("not a url", None),
    ],
)
def test_domain_from_url(url: str, expected: str | None) -> None:
    assert _domain_from_url(url) == expected


@pytest.mark.parametrize(
    ("domain", "blocked"),
    [
        ("housing.com", True),
        ("in.linkedin.com", True),
        ("tatahousing.com", False),  # live false positive: contains "housing.com"
        ("kumarx.com", False),  # contains "x.com"
    ],
)
def test_is_blocked_matches_domain_or_subdomain_only(domain: str, blocked: bool) -> None:
    assert _is_blocked(domain) is blocked


def test_company_from_result_maps_entity_fields() -> None:
    company = company_from_result(_result("https://www.koltepatil.com/", entity=_entity("Kolte-Patil", 1468)))
    assert company is not None
    assert company["id"] == "koltepatil.com"
    assert company["domain"] == "koltepatil.com"
    assert company["name"] == "Kolte-Patil"
    assert company["employee_count"] == 1468
    assert (company["city"], company["state"], company["country"]) == ("Pune", None, "India")
    assert company["short_description"] == "Residential real estate developer"
    assert company["industry"] is None


def test_exa_profile_url_falls_back_to_name_id() -> None:
    result = _result("https://exa.ai/library/organization/4ftgwg1n01n", entity=_entity("Gera Developments"))
    company = company_from_result(result)
    assert company is not None
    assert company["domain"] is None
    assert company["id"] == "name:gera developments"


@pytest.mark.parametrize(
    "entity",
    [
        None,
        {"description": "no name"},
        {"name": "  "},
    ],
)
def test_result_without_usable_entity_is_not_a_company(entity: dict | None) -> None:
    result = {"url": "https://x.in", "title": "t", "content": "", "entity": entity}
    assert company_from_result(result) is None


def test_missing_or_malformed_firmographics_become_none() -> None:
    entity = {"name": "Sparse Co", "workforce": "lots", "headquarters": None}
    company = company_from_result({"url": "https://sparse.in", "entity": entity})
    assert company is not None
    assert company["employee_count"] is None
    assert company["city"] is None
    assert company["country"] is None


def test_filters_blocklisted_junk_irrelevant_and_entityless_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_exa(query: str, **_: object) -> list[dict]:
        return [
            _result("https://www.good-realty.in", entity=_entity("Good Realty")),
            _result("https://in.linkedin.com/company/x"),  # blocklisted subdomain
            _result("https://jobs.example.com", title="Walk-in interview for real estate developer"),
            {  # off-topic, and its entity description isn't about real estate either
                "url": "https://www.billboard.com", "title": "Top charts this week", "content": "",
                "entity": {"name": "Billboard", "description": "Music charts"},
            },
            {"url": "https://no-entity.in", "title": "Real estate developer", "content": "", "entity": None},
            _result("https://good-realty.in/about", entity=_entity("Good Realty")),  # duplicate domain
        ]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    companies = discover_companies(ICP)
    assert [c["id"] for c in companies] == ["good-realty.in"]


def test_limit_bounds_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_exa(query: str, **_: object) -> list[dict]:
        calls.append(query)
        return [_result(f"https://dev{i}-{len(calls)}.in") for i in range(8)]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    assert len(discover_companies(ICP, limit=3)) == 3
    assert len(calls) == 1  # stops before searching the next geography


def test_one_failed_geography_does_not_abort_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exa(query: str, **_: object) -> list[dict]:
        if "Mumbai" in query:
            raise requests.exceptions.ConnectionError("down")
        return [_result("https://pune-realty.in", title="Real estate developer in Pune")]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    assert [c["id"] for c in discover_companies(ICP)] == ["pune-realty.in"]


def test_store_companies_upserts(temp_db: Path) -> None:
    company = company_from_result(_result("https://acme.in", entity=_entity("Acme", 100)))
    assert company is not None
    assert store_companies([company]) == ["acme.in"]

    company["employee_count"] = 150
    store_companies([company])

    conn = get_connection()
    rows = conn.execute("SELECT * FROM companies").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0]["employee_count"] == 150
    assert json.loads(rows[0]["raw_json"])["name"] == "Acme"
