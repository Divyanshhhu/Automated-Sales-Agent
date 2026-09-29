import pytest
from fastapi.testclient import TestClient
from markupsafe import escape

from src import contacts, outreach
from src.contacts import add_manual_contact, list_contacts
from src.outreach import get_draft, list_drafts
from src.profiles import Profile, create_profile
from tests.conftest import VALID_CONFIG, insert_company, insert_evidence, insert_memo


@pytest.fixture
def web_profile(client: TestClient) -> Profile:
    config = {**VALID_CONFIG, "icp": {**VALID_CONFIG["icp"], "roles": ["Head of Sales"]}}
    return create_profile("Web", config)


@pytest.fixture
def memo(web_profile: Profile) -> dict:
    insert_company("a.in", "Alpha Realty")
    evidence_id = insert_evidence(web_profile.id, "a.in", fact="Two new towers")
    memo_id = insert_memo(
        web_profile.id,
        "a.in",
        why=f"Two launches [E{evidence_id}].",
        status="approved",
        evidence_ids=[evidence_id],
    )
    return {"id": memo_id, "evidence": evidence_id}


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch, memo: dict) -> None:
    monkeypatch.setattr(
        outreach,
        "_call_llm",
        lambda *_: {
            "subject": "Your launches",
            "body": f"Two towers [E{memo['evidence']}]. We reply fast [PRODUCT].",
        },
    )


def test_unapproved_memo_explains_how_to_start(client: TestClient, web_profile: Profile) -> None:
    insert_company("b.in", "Beta")
    memo_id = insert_memo(web_profile.id, "b.in")
    page = client.get(f"/memos/{memo_id}").text
    assert "Approve this memo to find people" in page
    assert "/find-people" not in page
    assert client.post(f"/memos/{memo_id}/find-people").status_code == 409


def test_find_people(client: TestClient, memo: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_exa(query: str, **_: object) -> list[dict]:
        return [
            {
                "url": "https://www.linkedin.com/in/asha",
                "entity": {
                    "name": "Asha Rao",
                    "workHistory": [
                        {"title": "Head of Sales", "company": {"name": "Alpha Realty"}, "dates": {"to": None}}
                    ],
                },
            }
        ]

    monkeypatch.setattr(contacts, "exa_search", fake_exa)
    response = client.post(f"/memos/{memo['id']}/find-people", follow_redirects=False)
    assert response.status_code == 303
    page = client.get(response.headers["location"]).text
    assert "Found 1 new person to contact" in page
    assert "Asha Rao" in page and "Head of Sales" in page

    again = client.post(f"/memos/{memo['id']}/find-people")
    assert "No new people found in the target roles (1 already saved)" in again.text


def test_people_search_failure_is_shown(
    client: TestClient, memo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object, **__: object) -> list[dict]:
        raise RuntimeError("exa down")

    monkeypatch.setattr(contacts, "exa_search", broken)
    response = client.post(f"/memos/{memo['id']}/find-people")
    assert response.status_code == 502
    assert "People search failed" in response.text


def test_add_edit_delete_person(client: TestClient, memo: dict) -> None:
    response = client.post(f"/memos/{memo['id']}/contacts", data={"name": "Ravi", "email": "bad"})
    assert response.status_code == 422
    assert "doesn&#39;t look like an email address" in response.text

    client.post(f"/memos/{memo['id']}/contacts", data={"name": "Ravi", "title": "VP Sales"})
    (person,) = list_contacts("a.in")
    client.post(
        f"/memos/{memo['id']}/contacts/{person.id}",
        data={"name": "Ravi K", "title": "VP Sales", "email": "ravi@alpha.in", "profile_url": ""},
    )
    assert list_contacts("a.in")[0].email == "ravi@alpha.in"

    response = client.post(f"/memos/{memo['id']}/contacts/{person.id}/delete", follow_redirects=False)
    assert response.status_code == 303
    assert list_contacts("a.in") == []


def test_person_names_from_the_web_cannot_inject_script(client: TestClient, memo: dict) -> None:
    add_manual_contact("a.in", name="x');alert(1);('<b>")
    page = client.get(f"/memos/{memo['id']}").text
    assert "alert(1);('<b>" not in page  # never raw
    assert str(escape("x');alert(1);('<b>")) in page  # only as escaped text / attribute value
    assert "confirm('" not in page  # no names built into script


def test_write_approve_and_send_email(client: TestClient, memo: dict, fake_llm: None) -> None:
    person = add_manual_contact("a.in", name="Asha Rao", email="asha@alpha.in")

    response = client.post(f"/memos/{memo['id']}/contacts/{person.id}/draft", follow_redirects=False)
    assert response.status_code == 303
    draft_url = response.headers["location"].split("?")[0]
    page = client.get(response.headers["location"]).text
    assert "Email written" in page
    assert "Hi Asha," in page
    assert f'href="#E{memo["evidence"]}"' in page  # the checked version links its evidence
    assert "about your product" in page

    (draft,) = list_drafts()
    client.post(draft_url, data={"subject": "Edited subject", "body": "Hi Asha,\n\nEdited body."})
    assert get_draft(draft.id).subject == "Edited subject"

    page = client.post(f"{draft_url}/approve").text
    assert "Email approved" in page
    assert "mailto:asha@alpha.in?subject=Edited%20subject" in page

    client.post(f"{draft_url}/sent")
    assert get_draft(draft.id).status == "sent"
    assert "Sent" in client.get("/outreach?status=sent").text

    memo_page = client.get(f"/memos/{memo['id']}").text
    assert "badge-sent" in memo_page


def test_invalid_draft_actions_are_refused(client: TestClient, memo: dict, fake_llm: None) -> None:
    person = add_manual_contact("a.in", name="Asha Rao")
    client.post(f"/memos/{memo['id']}/contacts/{person.id}/draft")
    (draft,) = list_drafts()
    response = client.post(f"/drafts/{draft.id}/sent")
    assert response.status_code == 409
    assert "Approve the email before marking it as sent" in response.text


def test_email_generation_failure_is_shown(
    client: TestClient, memo: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_: object) -> dict:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(outreach, "_call_llm", broken)
    person = add_manual_contact("a.in", name="Asha Rao")
    response = client.post(f"/memos/{memo['id']}/contacts/{person.id}/draft")
    assert response.status_code == 502
    assert "Writing the email failed" in response.text
    assert list_drafts() == []


def test_outreach_page_empty_state(client: TestClient) -> None:
    assert "No emails here yet" in client.get("/outreach").text


def test_missing_things_are_404(client: TestClient) -> None:
    assert client.get("/drafts/999").status_code == 404
    assert client.post("/drafts/999/approve").status_code == 404
