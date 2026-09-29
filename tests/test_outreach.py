from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

import pytest

from src import outreach
from src.contacts import Contact, add_manual_contact, delete_contact
from src.db import get_connection
from src.outreach import (
    DraftNotFoundError,
    OutreachError,
    approve_draft,
    compose_email,
    drafts_for_memo,
    generate_draft,
    get_draft,
    list_drafts,
    mark_sent,
    tracked_link,
    update_draft,
)
from src.profiles import Profile, update_profile
from src.review import set_review
from tests.conftest import insert_company, insert_evidence, insert_memo


@pytest.fixture
def approved(profile: Profile, company_row: dict) -> dict:
    """An approved memo citing one of two evidence items, and a contact."""
    cited = insert_evidence(profile.id, "c1", fact="Launched two towers in 2026", url="https://news/1")
    uncited = insert_evidence(profile.id, "c1", fact="Hiring a CRM head", url="https://news/2")
    memo_id = insert_memo(
        profile.id, "c1", why=f"Two launches [E{cited}].", status="approved", evidence_ids=[cited, uncited]
    )
    contact = add_manual_contact("c1", name="Asha Rao", title="VP Sales", email="asha@acme.in")
    return {"memo": memo_id, "contact": contact, "cited": cited, "uncited": uncited}


def _fake_llm(body: str, subject: str = "Your two new launches") -> tuple[Callable[..., dict], dict]:
    seen: dict = {}

    def fake(
        product: dict, contact: Contact, company_name: str, memo_text: str, evidence: list[dict]
    ) -> dict:
        seen.update(contact=contact.name, company=company_name, evidence=[e["id"] for e in evidence])
        return {"subject": subject, "body": body}

    return fake, seen


# ---------- building the email ----------


def test_tracked_link_adds_utm_and_keeps_existing_query() -> None:
    link = tracked_link("https://calendly.com/me/15min?month=2026-10", "SA-ABC123")
    query = parse_qs(urlparse(link).query)
    assert query["month"] == ["2026-10"]
    assert query["utm_content"] == ["SA-ABC123"]
    assert query["utm_source"] == ["sales-agent"]


@pytest.mark.parametrize(
    ("url", "expected_text"),
    [
        ("https://wa.me/919999999999", "Hi, I'd like to see how it works (ref SA-1)"),
        ("https://wa.me/919999999999?text=Hello%20there", "Hello there (ref SA-1)"),
        ("https://api.whatsapp.com/send?phone=91999", "Hi, I'd like to see how it works (ref SA-1)"),
    ],
)
def test_whatsapp_links_carry_ref_in_prefilled_message(url: str, expected_text: str) -> None:
    assert parse_qs(urlparse(tracked_link(url, "SA-1")).query)["text"] == [expected_text]


def test_compose_email_with_link_and_signature() -> None:
    email = compose_email(
        "Asha",
        "Body text.",
        {"cta_url": "https://cal.com/x", "cta_label": "Book a call", "signature": "Divyanshu\nFounder"},
        "SA-1",
    )
    parts = email.split("\n\n")
    assert parts[0] == "Hi Asha,"
    assert parts[1] == "Body text."
    assert parts[2].startswith("Book a call: https://cal.com/x?")
    assert parts[3] == "Divyanshu\nFounder"


def test_compose_email_without_link_asks_for_reply() -> None:
    email = compose_email("", "Body.", {}, "SA-1")
    assert email.startswith("Hello,")
    assert "Just reply to this email." in email


# ---------- generating ----------


def test_generate_draft(profile: Profile, approved: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    update_profile(
        profile.id,
        config={**profile.config, "outreach": {"cta_url": "https://cal.com/x", "sender_name": "Divyanshu"}},
    )
    body = f"You launched two towers [E{approved['cited']}]. Our agent replies in seconds [PRODUCT]."
    fake, seen = _fake_llm(body)
    monkeypatch.setattr(outreach, "_call_llm", fake)

    draft = generate_draft(approved["memo"], approved["contact"].id)

    # only evidence the approved memo cited is offered to the model
    assert seen == {"contact": "Asha Rao", "company": "Acme Realty", "evidence": [approved["cited"]]}
    assert draft.generated_body == body
    assert draft.citation_issues == []
    assert draft.status == "draft"
    assert draft.ref_code.startswith("SA-")
    assert draft.body.startswith("Hi Asha,\n\nYou launched two towers. Our agent replies in seconds.")
    assert f"utm_content={draft.ref_code}" in draft.body
    assert draft.body.endswith("Divyanshu")
    assert "[E" not in draft.body and "[PRODUCT]" not in draft.body


def test_uncited_or_unoffered_evidence_is_flagged(approved: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    body = f"You are growing fast. You are hiring a CRM head [E{approved['uncited']}]."
    fake, _ = _fake_llm(body, subject="Growth [INFERENCE]")
    monkeypatch.setattr(outreach, "_call_llm", fake)
    draft = generate_draft(approved["memo"], approved["contact"].id)
    assert len(draft.citation_issues) == 3
    assert draft.subject == "Growth"


def test_memo_must_be_approved(approved: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    set_review(approved["memo"], "reset")
    with pytest.raises(OutreachError, match="Approve the memo"):
        generate_draft(approved["memo"], approved["contact"].id)


def test_contact_must_work_at_the_memo_company(profile: Profile, approved: dict) -> None:
    insert_company("other.in", "Other")
    stranger = add_manual_contact("other.in", name="Stranger")
    with pytest.raises(OutreachError, match="doesn't work"):
        generate_draft(approved["memo"], stranger.id)


# ---------- lifecycle ----------


def test_lifecycle_edit_approve_send(approved: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _fake_llm(f"Two launches [E{approved['cited']}].")
    monkeypatch.setattr(outreach, "_call_llm", fake)
    draft = generate_draft(approved["memo"], approved["contact"].id)

    with pytest.raises(OutreachError, match="Approve the email"):
        mark_sent(draft.id)
    approved_draft = approve_draft(draft.id)
    assert approved_draft.status == "approved" and approved_draft.approved_at

    edited = update_draft(draft.id, subject="New subject", body="Hi Asha,\n\nEdited.")
    assert edited.status == "draft"  # the approval was for the old text
    assert edited.approved_at is None

    approve_draft(draft.id)
    sent = mark_sent(draft.id)
    assert sent.status == "sent" and sent.sent_at

    with pytest.raises(OutreachError, match="already sent"):
        update_draft(draft.id, subject="x", body="y")
    with pytest.raises(OutreachError, match="already sent"):
        generate_draft(approved["memo"], approved["contact"].id)


def test_rewrite_keeps_ref_code_and_clears_approval(approved: dict, monkeypatch: pytest.MonkeyPatch) -> None:
    fake, _ = _fake_llm(f"Two launches [E{approved['cited']}].")
    monkeypatch.setattr(outreach, "_call_llm", fake)
    first = generate_draft(approved["memo"], approved["contact"].id)
    approve_draft(first.id)
    second = generate_draft(approved["memo"], approved["contact"].id)
    assert (second.id, second.ref_code, second.status) == (first.id, first.ref_code, "draft")


@pytest.mark.parametrize(("subject", "body"), [("", "x"), ("x", "   "), ("x" * 151, "y")])
def test_invalid_edits_rejected(
    approved: dict, monkeypatch: pytest.MonkeyPatch, subject: str, body: str
) -> None:
    fake, _ = _fake_llm(f"Two launches [E{approved['cited']}].")
    monkeypatch.setattr(outreach, "_call_llm", fake)
    draft = generate_draft(approved["memo"], approved["contact"].id)
    with pytest.raises(OutreachError):
        update_draft(draft.id, subject=subject, body=body)


def test_listing_and_deleting_the_contact_deletes_drafts(
    profile: Profile, approved: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake, _ = _fake_llm(f"Two launches [E{approved['cited']}].")
    monkeypatch.setattr(outreach, "_call_llm", fake)
    draft = generate_draft(approved["memo"], approved["contact"].id)

    assert [d.id for d in list_drafts(profile.id)] == [draft.id]
    assert list_drafts(profile.id, "sent") == []
    assert set(drafts_for_memo(approved["memo"])) == {approved["contact"].id}

    delete_contact(approved["contact"].id)
    with pytest.raises(DraftNotFoundError):
        get_draft(draft.id)
    conn = get_connection()
    assert conn.execute("SELECT COUNT(*) FROM outreach_drafts").fetchone()[0] == 0
    conn.close()
