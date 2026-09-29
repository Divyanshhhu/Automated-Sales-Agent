import csv
import io

import pytest
from fastapi.testclient import TestClient

from src import memo_generator
from src.confidence import LABELS
from src.profiles import Profile, create_profile
from src.review import get_memo
from tests.conftest import VALID_CONFIG, insert_company, insert_evidence, insert_memo


@pytest.fixture
def web_profile(client: TestClient) -> Profile:
    return create_profile("Web", dict(VALID_CONFIG))


@pytest.fixture
def memo_ids(web_profile: Profile) -> dict[str, int]:
    insert_company("a.in", "Alpha")
    insert_company("b.in", "Beta")
    evidence_id = insert_evidence(web_profile.id, "a.in", fact="Launched <b>3</b> towers",
                                  url="javascript:alert(1)")
    return {
        "evidence": evidence_id,
        "alpha": insert_memo(web_profile.id, "a.in", score=90, why=f"Growing fast [E{evidence_id}].",
                             evidence_ids=[evidence_id]),
        "beta": insert_memo(web_profile.id, "b.in", score=60, confidence=LABELS["strong"]),
    }


def test_review_page_lists_memos_awaiting_review(client: TestClient, memo_ids: dict[str, int]) -> None:
    response = client.get("/review")
    assert response.status_code == 200
    assert response.text.index("Alpha") < response.text.index("Beta")  # higher score first
    assert "To review (2)" in response.text


def test_review_page_filters(client: TestClient, web_profile: Profile, memo_ids: dict[str, int]) -> None:
    response = client.get(f"/review?profile_id={web_profile.id}&status=all&confidence=strong")
    assert "Beta" in response.text
    assert "Alpha" not in response.text


def test_review_page_without_profiles(client: TestClient) -> None:
    assert "No profiles yet" in client.get("/review").text


def test_memo_page_renders_citations_and_neutralizes_untrusted_content(
    client: TestClient, memo_ids: dict[str, int]
) -> None:
    evidence_id = memo_ids["evidence"]
    page = client.get(f"/memos/{memo_ids['alpha']}").text
    assert f'<a class="cite" href="#E{evidence_id}">[E{evidence_id}]</a>' in page
    assert f'id="E{evidence_id}"' in page
    assert "&lt;b&gt;3&lt;/b&gt;" in page  # evidence text escaped
    assert 'href="javascript:' not in page  # unsafe source URL never becomes a link


def test_approve_goes_to_next_memo(client: TestClient, memo_ids: dict[str, int]) -> None:
    response = client.post(
        f"/memos/{memo_ids['alpha']}/review",
        data={"decision": "approved", "notes": "strong fit", "advance": "1"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == f"/memos/{memo_ids['beta']}?done={memo_ids['alpha']}"
    assert "Saved: Alpha marked approved." in client.get(response.headers["location"]).text
    memo = get_memo(memo_ids["alpha"])
    assert (memo.review_status, memo.review_notes) == ("approved", "strong fit")


def test_last_decision_returns_to_the_list(
    client: TestClient, web_profile: Profile, memo_ids: dict[str, int]
) -> None:
    client.post(f"/memos/{memo_ids['alpha']}/review", data={"decision": "approved"})
    response = client.post(
        f"/memos/{memo_ids['beta']}/review",
        data={"decision": "rejected", "advance": "1"},
        follow_redirects=False,
    )
    assert response.headers["location"] == f"/review?profile_id={web_profile.id}&status=all"


def test_decision_without_advance_stays_and_can_be_undone(
    client: TestClient, memo_ids: dict[str, int]
) -> None:
    memo_id = memo_ids["beta"]
    response = client.post(f"/memos/{memo_id}/review", data={"decision": "rejected"}, follow_redirects=False)
    assert response.headers["location"] == f"/memos/{memo_id}?saved=1"
    assert "Undo decision" in client.get(f"/memos/{memo_id}").text

    client.post(f"/memos/{memo_id}/review", data={"decision": "reset"})
    assert get_memo(memo_id).review_status == "pending"


def test_unknown_decision_is_rejected(client: TestClient, memo_ids: dict[str, int]) -> None:
    response = client.post(f"/memos/{memo_ids['alpha']}/review", data={"decision": "delete"})
    assert response.status_code == 422
    assert get_memo(memo_ids["alpha"]).review_status == "pending"


def test_regenerate(client: TestClient, memo_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch) -> None:
    evidence_id = memo_ids["evidence"]
    monkeypatch.setattr(
        memo_generator,
        "_call_llm",
        lambda *_: {"why_relevant": f"Rewritten [E{evidence_id}].", "potential_use_case": "Use [INFERENCE]."},
    )
    response = client.post(f"/memos/{memo_ids['alpha']}/regenerate", follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Memo regenerated" in page
    assert "Rewritten" in page


def test_regenerate_failure_is_shown_and_memo_kept(
    client: TestClient, memo_ids: dict[str, int], monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> dict:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(memo_generator, "_call_llm", broken)
    response = client.post(f"/memos/{memo_ids['alpha']}/regenerate")
    assert response.status_code == 502
    assert "Regeneration failed" in response.text
    assert "Growing fast" in response.text


def test_csv_download(client: TestClient, web_profile: Profile, memo_ids: dict[str, int]) -> None:
    client.post(f"/memos/{memo_ids['alpha']}/review", data={"decision": "approved", "notes": "=cmd|calc"})
    response = client.get(f"/profiles/{web_profile.id}/memos.csv")
    assert response.status_code == 200
    assert 'filename="memos_web.csv"' in response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(response.content.decode("utf-8-sig"))))
    assert {r["company_name"] for r in rows} == {"Alpha", "Beta"}
    alpha = next(r for r in rows if r["company_name"] == "Alpha")
    assert alpha["review_status"] == "approved"
    assert alpha["review_notes"] == "'=cmd|calc"  # formula neutralized for Excel


def test_missing_memo_is_404(client: TestClient) -> None:
    assert client.get("/memos/999").status_code == 404
    assert client.post("/memos/999/regenerate").status_code == 404
