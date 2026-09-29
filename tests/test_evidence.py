import pytest
import requests

from src import evidence
from src.db import get_connection
from src.evidence import _is_genuine_pain_point, get_evidence_for_company, retrieve_evidence_for_company
from src.profiles import Profile

TAXONOMY = {
    "expansion_launch": "{company} launch",
    "direct_pain_point": "{company} complaint",
}


@pytest.mark.parametrize(
    "text",
    [
        "Buyers complain of slow response from the sales team.",
        "Several customers reported delayed follow-up after site visits.",
        # live MouthShut review of Adani Realty
        "No update on why possession is being delayed. They take so much time for possession",
        "no one is not responding my query",
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
        # live complaint-site boilerplate: "complaint" + "customer care" is not a complaint
        "Having problems with Tata Housing Development? File a complaint and get it resolved by "
        "Tata Housing Development customer care. It's quick, effective",
        # a delay, but not about responsiveness
        "Customers are unhappy that possession is delayed by two years",
    ],
)
def test_non_pain_point_rejected(text: str) -> None:
    assert not _is_genuine_pain_point(text)


@pytest.mark.parametrize(
    ("result", "company", "expected"),
    [  # live results from the complaint-site probe
        ({"url": "https://www.mouthshut.com/builders-and-developers/adani-realty-reviews-926205746"},
         "Adani Realty", True),
        ({"url": "https://www.mouthshut.com/builders-and-developers/marathon-realty-reviews-925927335"},
         "Adani Realty", False),
        ({"url": "https://www.consumercomplaints.in/tata-housing-development-b100864"},
         "Tata Housing Development Company Limited", True),
        ({"url": "https://voxya.com/consumer-complaints/housing-loan-emi-issue/93491",
          "content": "no one is not responding my query"}, "Tata Housing Development Company Limited", False),
        ({"title": "How is Kalpataru Parkcity", "url": "https://www.reddit.com/r/thane/x"},
         "Rustomjee", False),
        ({"content": "Rustomjee never replied to my emails"}, "Rustomjee", True),
    ],
)
def test_mentions_company(result: dict, company: str, expected: bool) -> None:
    assert evidence.mentions_company(result, company) is expected


def test_pain_point_searches_complaint_sites_and_keeps_only_this_company(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict] = []

    def fake_tavily(query: str, **kwargs: object) -> list[dict]:
        calls.append(kwargs)
        return [
            {"content": "Acme Realty never replied to my follow up emails.", "url": "https://mouthshut.com/acme"},
            {"content": "Marathon never replied to my follow up emails.", "url": "https://mouthshut.com/marathon"},
            {"content": "Acme Realty launched a tower.", "url": "https://mouthshut.com/acme-2"},
        ]

    monkeypatch.setattr(evidence, "tavily_search", fake_tavily)
    taxonomy = {"direct_pain_point": "{company} x"}
    stored = retrieve_evidence_for_company(profile.id, "c1", "Acme Realty", taxonomy)
    assert stored == 1
    assert calls[0]["include_domains"] == evidence.PAIN_POINT_SITES
    assert calls[0]["max_results"] == evidence.PAIN_POINT_RESULTS
    assert "time_range" not in calls[0]


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


def test_retrieval_stores_results(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tavily_search", FakeTavily())
    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", TAXONOMY) == 4
    categories = {e["category"] for e in get_evidence_for_company(profile.id, "c1")}
    assert categories == {"expansion_launch", "direct_pain_point"}


def test_rerun_does_not_duplicate_or_requery(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeTavily()
    monkeypatch.setattr(evidence, "tavily_search", fake)
    retrieve_evidence_for_company(profile.id, "c1", "Acme", TAXONOMY)
    queries_after_first_run = len(fake.queries)

    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", TAXONOMY) == 0
    assert _evidence_count("c1") == 4
    assert len(fake.queries) == queries_after_first_run  # no Tavily credits spent on the rerun


def test_rerun_after_partial_failure_fills_only_the_gap(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(evidence, "tavily_search", FakeTavily(fail_for={"complaint"}))
    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", TAXONOMY) == 2

    recovered = FakeTavily()
    monkeypatch.setattr(evidence, "tavily_search", recovered)
    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", TAXONOMY) == 2
    assert recovered.queries == ["Acme complaint"]
    assert _evidence_count("c1") == 4


def test_duplicate_url_within_category_is_ignored(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def same_url_twice(query: str, max_results: int = 5, **_: object) -> list[dict]:
        return [{"content": "launch news", "url": "https://same"}] * 2

    monkeypatch.setattr(evidence, "tavily_search", same_url_twice)
    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", {"expansion_launch": "{company}"}) == 1


def test_non_genuine_pain_point_results_are_filtered(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def launch_news(query: str, max_results: int = 5, **_: object) -> list[dict]:
        return [{"content": "The company launched a new tower.", "url": "https://news"}]

    monkeypatch.setattr(evidence, "tavily_search", launch_news)
    assert retrieve_evidence_for_company(profile.id, "c1", "Acme", {"direct_pain_point": "{company}"}) == 0
