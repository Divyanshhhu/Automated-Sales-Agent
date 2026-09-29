from pathlib import Path

import pytest
import requests

from src import evidence
from src.db import get_connection
from src.evidence import _is_genuine_pain_point, get_evidence_for_company, retrieve_evidence_for_company

TAXONOMY = {
    "expansion_launch": "{company} launch",
    "direct_pain_point": "{company} complaint",
}


@pytest.mark.parametrize(
    "text",
    [
        "Buyers complain of slow response from the sales team.",
        "Several customers reported delayed follow-up after site visits.",
    ],
)
def test_genuine_pain_point_detected(text: str) -> None:
    assert _is_genuine_pain_point(text)


@pytest.mark.parametrize(
    "text",
    [
        # the live false positive: both terms present, but in different clauses
        "Strong customer collections this quarter. Government approval-related delays hit two projects.",
        "The company launched three new towers in Pune.",
    ],
)
def test_non_pain_point_rejected(text: str) -> None:
    assert not _is_genuine_pain_point(text)


class FakeTavily:
    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.queries: list[str] = []
        self.fail_for = fail_for or set()

    def __call__(self, query: str, max_results: int = 5, **_: object) -> list[dict]:
        self.queries.append(query)
        if any(marker in query for marker in self.fail_for):
            raise requests.exceptions.ConnectionError("simulated outage")
        return [
            {"content": f"Buyers complain of slow response ({query})", "url": f"https://src/{query}/1"},
            {"content": f"Customers report delayed follow up ({query})", "url": f"https://src/{query}/2"},
        ]


def _evidence_count(company_id: str) -> int:
    conn = get_connection()
    count = conn.execute(
        "SELECT COUNT(*) FROM evidence_items WHERE company_id=?", (company_id,)
    ).fetchone()[0]
    conn.close()
    return count


def test_retrieval_stores_results(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(evidence, "tavily_search", FakeTavily())
    assert retrieve_evidence_for_company("c1", "Acme", TAXONOMY) == 4
    categories = {e["category"] for e in get_evidence_for_company("c1")}
    assert categories == {"expansion_launch", "direct_pain_point"}


def test_rerun_does_not_duplicate_or_requery(temp_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeTavily()
    monkeypatch.setattr(evidence, "tavily_search", fake)
    retrieve_evidence_for_company("c1", "Acme", TAXONOMY)
    queries_after_first_run = len(fake.queries)

    assert retrieve_evidence_for_company("c1", "Acme", TAXONOMY) == 0
    assert _evidence_count("c1") == 4
    assert len(fake.queries) == queries_after_first_run  # no Tavily credits spent on the rerun


def test_rerun_after_partial_failure_fills_only_the_gap(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tavily_search", FakeTavily(fail_for={"complaint"}))
    assert retrieve_evidence_for_company("c1", "Acme", TAXONOMY) == 2

    recovered = FakeTavily()
    monkeypatch.setattr(evidence, "tavily_search", recovered)
    assert retrieve_evidence_for_company("c1", "Acme", TAXONOMY) == 2
    assert recovered.queries == ["Acme complaint"]
    assert _evidence_count("c1") == 4


def test_duplicate_url_within_category_is_ignored(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def same_url_twice(query: str, max_results: int = 5, **_: object) -> list[dict]:
        return [{"content": "launch news", "url": "https://same"}] * 2

    monkeypatch.setattr(evidence, "tavily_search", same_url_twice)
    assert retrieve_evidence_for_company("c1", "Acme", {"expansion_launch": "{company}"}) == 1


def test_non_genuine_pain_point_results_are_filtered(
    temp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch_news(query: str, max_results: int = 5, **_: object) -> list[dict]:
        return [{"content": "The company launched a new tower.", "url": "https://news"}]

    monkeypatch.setattr(evidence, "tavily_search", launch_news)
    assert retrieve_evidence_for_company("c1", "Acme", {"direct_pain_point": "{company}"}) == 0
