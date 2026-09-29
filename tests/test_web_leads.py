import csv
import io
import json

import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from src import evidence, memo_generator
from src.confidence import LABELS
from src.db import get_connection
from src.profiles import Profile, create_profile
from src.review import get_memo
from src.web.forms import config_to_form
from tests.conftest import VALID_CONFIG, insert_company, insert_evidence, insert_memo


def _score(profile_id: int, company_id: str, score: float, breakdown: dict) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)"
        " VALUES (?, ?, ?, 'x', ?, '2026-01-01')",
        (profile_id, company_id, score, json.dumps(breakdown)),
    )
    conn.commit()
    conn.close()


@pytest.fixture
def web_profile(client: TestClient) -> Profile:
    return create_profile("Web", dict(VALID_CONFIG))


@pytest.fixture
def ids(web_profile: Profile) -> dict[str, int]:
    """Alpha (90) and Beta (60) waiting for review; Giant scored too large to fit."""
    insert_company("a.in", "Alpha")
    insert_company("b.in", "Beta")
    insert_company("g.in", "Giant", employee_count=9000)
    _score(web_profile.id, "a.in", 90, {"employee": 30, "location": 40, "industry": 20})
    _score(web_profile.id, "b.in", 60, {"employee": 30, "location": 40, "industry": 0})
    _score(web_profile.id, "g.in", 40, {"employee": 0, "location": 40, "industry": 0})
    evidence_id = insert_evidence(
        web_profile.id, "a.in", fact="Launched <b>3</b> towers", url="javascript:alert(1)"
    )
    return {
        "evidence": evidence_id,
        "alpha": insert_memo(
            web_profile.id,
            "a.in",
            score=90,
            evidence_ids=[evidence_id],
            why=f"Launched three towers [E{evidence_id}]. Enquiries will grow [INFERENCE]. "
            "The sources don't show response times [NO_EVIDENCE].",
        ),
        "beta": insert_memo(web_profile.id, "b.in", score=60, confidence=LABELS["strong"]),
    }


# ---------- Home ----------


def test_home_shows_pipeline_and_next_steps(client: TestClient, ids: dict[str, int]) -> None:
    page = " ".join(client.get("/").text.split())
    assert "Your pipeline" in page
    assert "2 companies are waiting for your review" in page
    assert f'href="/leads/{ids["alpha"]}"' in page  # "Start reviewing" opens the best one first
    assert "Pune" in page and "haven&#39;t been searched yet" in page  # only Mumbai has companies
    assert "up to $1.06" in page  # estimate for the default 25 companies
    assert "Spending this month" in page


def test_estimate_updates_with_the_number(client: TestClient) -> None:
    page = " ".join(client.get("/estimate?limit=10").text.split())
    assert "50 credits, 5% of your free month" in page
    assert "about $0.40" in page


# ---------- Leads ----------


def test_leads_default_to_what_needs_review(client: TestClient, ids: dict[str, int]) -> None:
    page = client.get("/leads").text
    assert page.index("Alpha") < page.index("Beta")
    assert "Giant" not in page
    assert "Possible need" in page


def test_not_a_fit_explains_why(client: TestClient, web_profile: Profile, ids: dict[str, int]) -> None:
    page = client.get(f"/leads?campaign={web_profile.id}&stage=notfit").text
    assert "Giant" in page
    assert "Larger than your 51–2,000 range" in page


def test_leads_search(client: TestClient, web_profile: Profile, ids: dict[str, int]) -> None:
    page = client.get(f"/leads?campaign={web_profile.id}&stage=all&q=bet").text
    assert "Beta" in page and "Alpha" not in page


def test_old_addresses_redirect(client: TestClient, ids: dict[str, int]) -> None:
    assert client.get("/review", follow_redirects=False).headers["location"] == "/leads"
    old = client.get(f"/memos/{ids['alpha']}", follow_redirects=False)
    assert old.headers["location"] == f"/leads/{ids['alpha']}"


# ---------- company page ----------


def test_company_page_reads_like_the_mockup(client: TestClient, ids: dict[str, int]) -> None:
    page = client.get(f"/leads/{ids['alpha']}").text
    assert '<sup class="fn"><a href="#source-1">1</a></sup>' in page  # footnote, not [E..]
    assert "[E" not in page.split("<main")[1]
    assert "our reading" in page
    assert "Not shown by the sources:" in page and "response times" in page
    assert "&lt;b&gt;3&lt;/b&gt;" in page  # source text escaped
    assert 'href="javascript:' not in page  # unsafe source URL never becomes a link
    assert 'data-shortcut="a"' in page and 'data-shortcut="r"' in page and 'data-shortcut="j"' in page
    assert "Worth contacting?" in page


def test_unchecked_sources_offer_an_update(client: TestClient, ids: dict[str, int]) -> None:
    page = client.get(f"/leads/{ids['alpha']}").text
    assert "Not checked yet for this company:" in page
    assert "customer complaints" in page


def test_approve_stays_and_shows_the_next_step(client: TestClient, ids: dict[str, int]) -> None:
    response = client.post(
        f"/leads/{ids['alpha']}/decision",
        data={"decision": "approved", "notes": "strong fit"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/leads/{ids['alpha']}?done=approved"
    page = " ".join(client.get(response.headers["location"]).text.split())
    assert "Approved." in page and "Next to review:" in page and "Beta" in page
    assert "Approved. Who should you contact?" in page
    assert '<li class="current" aria-current="step"> <span class="mark">3</span>Find contact' in page
    assert get_memo(ids["alpha"]).review_notes == "strong fit"


def test_reject_moves_on_through_the_queue(client: TestClient, ids: dict[str, int]) -> None:
    first = client.post(
        f"/leads/{ids['alpha']}/decision", data={"decision": "rejected"}, follow_redirects=False
    )
    assert first.headers["location"] == f"/leads/{ids['beta']}"
    last = client.post(
        f"/leads/{ids['beta']}/decision", data={"decision": "rejected"}, follow_redirects=False
    )
    assert last.headers["location"] == "/leads?stage=review"
    assert "Marked as not a fit" in client.get(f"/leads/{ids['alpha']}").text


def test_unknown_decision_is_rejected(client: TestClient, ids: dict[str, int]) -> None:
    response = client.post(f"/leads/{ids['alpha']}/decision", data={"decision": "delete"})
    assert response.status_code == 422
    assert get_memo(ids["alpha"]).review_status == "pending"


def test_update_research_runs_missing_searches_and_records_cost(
    client: TestClient, web_profile: Profile, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src import tavily_client

    def fake_post(url: str, **kwargs: object) -> object:
        class Response:
            status_code = 200

            def raise_for_status(self) -> None:
                pass

            def json(self) -> dict:
                return {
                    "results": [
                        {
                            "content": "Alpha never replied to my follow up emails.",
                            "url": "https://mouthshut.com/alpha",
                        }
                    ]
                }

        return Response()

    monkeypatch.setenv("TAVILY_API_KEY", "test")
    monkeypatch.setattr(tavily_client.requests, "post", fake_post)
    monkeypatch.setattr(
        memo_generator,
        "_call_llm",
        lambda product, row, items: {
            "why_relevant": " ".join(f"Fact [E{e['id']}]." for e in items),
            "potential_use_case": "Use [INFERENCE].",
        },
    )
    response = client.post(f"/leads/{ids['alpha']}/update-research", follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Research updated — 2 new source(s) found." in page
    assert "Not checked yet" not in page
    assert get_memo(ids["alpha"]).signal.level == "strong"  # the complaint is now cited

    conn = get_connection()
    rows = conn.execute("SELECT provider, action, profile_id FROM api_usage").fetchall()
    conn.close()
    assert {(r["provider"], r["action"], r["profile_id"]) for r in rows} == {
        ("tavily", "update_research", web_profile.id)
    }


def test_update_research_failure_keeps_the_old_text(
    client: TestClient, ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> list:
        raise RuntimeError("tavily down")

    monkeypatch.setattr(evidence, "tavily_search", broken)
    monkeypatch.setattr(memo_generator, "_call_llm", broken)
    response = client.post(f"/leads/{ids['alpha']}/update-research")
    assert response.status_code == 502
    assert "Updating failed" in response.text
    assert "Launched three towers" in response.text


def test_saving_campaign_relabels_research(
    client: TestClient, web_profile: Profile, ids: dict[str, int]
) -> None:
    form = {
        **config_to_form(web_profile.config),
        "name": web_profile.name,
        "signals.likely_need_phrases": "towers",
    }
    client.post(f"/profiles/{web_profile.id}", data=form)
    assert get_memo(ids["alpha"]).signal_confidence == LABELS["likely"]


def test_csv_download(client: TestClient, web_profile: Profile, ids: dict[str, int]) -> None:
    client.post(f"/leads/{ids['alpha']}/decision", data={"decision": "approved", "notes": "=cmd|calc"})
    response = client.get(f"/profiles/{web_profile.id}/memos.csv")
    assert 'filename="leads_web.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    alpha = next(r for r in rows if r["company_name"] == "Alpha")
    assert alpha["review_notes"] == "'=cmd|calc"  # formula neutralized for Excel


def test_names_from_the_web_are_escaped_on_every_page(client: TestClient, web_profile: Profile) -> None:
    insert_company("x.in", "<script>alert(1)</script>")
    _score(web_profile.id, "x.in", 90, {})
    memo_id = insert_memo(web_profile.id, "x.in")
    for url in ("/", "/leads?stage=all", f"/leads/{memo_id}"):
        page = client.get(url).text
        assert "<script>alert(1)</script>" not in page
        assert str(escape("<script>alert(1)</script>")) in page


def test_missing_lead_is_404(client: TestClient) -> None:
    assert client.get("/leads/999").status_code == 404
    assert client.post("/leads/999/update-research").status_code == 404
