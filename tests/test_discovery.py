import pytest
import requests

from src import discovery
from src.discovery import _domain_from_url, find_candidate_domains

ICP = {"industry_keywords": ["real estate developer"], "geographies": ["Mumbai", "Pune"]}


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


def _result(url: str, title: str = "Real estate developer in Mumbai", content: str = "") -> dict:
    return {"url": url, "title": title, "content": content}


def test_filters_blocklisted_junk_and_irrelevant_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exa(query: str, **_: object) -> list[dict]:
        return [
            _result("https://www.good-realty.in"),
            _result("https://in.linkedin.com/company/x"),  # blocklisted subdomain
            _result("https://jobs.example.com", title="Walk-in interview for real estate developer"),
            _result("https://www.billboard.com", title="Top charts this week"),  # off-topic
            _result("https://good-realty.in/about"),  # duplicate domain
        ]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    assert find_candidate_domains(ICP) == ["good-realty.in"]


def test_max_candidates_bounds_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_exa(query: str, **_: object) -> list[dict]:
        calls.append(query)
        return [_result(f"https://dev{i}-{len(calls)}.in") for i in range(8)]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    assert len(find_candidate_domains(ICP, max_candidates=3)) == 3
    assert len(calls) == 1  # stops before searching the next geography


def test_one_failed_geography_does_not_abort_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exa(query: str, **_: object) -> list[dict]:
        if "Mumbai" in query:
            raise requests.exceptions.ConnectionError("down")
        return [_result("https://pune-realty.in", title="Real estate developer in Pune")]

    monkeypatch.setattr(discovery, "exa_search", fake_exa)
    assert find_candidate_domains(ICP) == ["pune-realty.in"]
