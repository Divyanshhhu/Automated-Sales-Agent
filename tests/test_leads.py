import json

from src.contacts import add_manual_contact
from src.db import get_connection
from src.leads import dashboard, fit_word, list_leads, review_queue, stage_counts
from src.profiles import Profile
from src.review import set_review
from tests.conftest import insert_company, insert_memo


def _score(profile_id: int, company_id: str, score: float, breakdown: dict | None = None) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)"
        " VALUES (?, ?, ?, 'x', ?, '2026-01-01')",
        (profile_id, company_id, score, json.dumps(breakdown or {})),
    )
    conn.commit()
    conn.close()


def _draft(memo_id: int, contact_id: int, status: str) -> None:
    conn = get_connection()
    conn.execute(
        "INSERT INTO outreach_drafts (memo_id, contact_id, ref_code, generated_body, subject, body, status,"
        " created_at, updated_at) VALUES (?, ?, ?, 'x', 's', 'b', ?, 't', 't')",
        (memo_id, contact_id, f"SA-{memo_id}{contact_id}", status),
    )
    conn.commit()
    conn.close()


def test_stages_and_next_steps(profile: Profile) -> None:
    for cid, score in (
        ("review.in", 90),
        ("find.in", 85),
        ("write.in", 80),
        ("send.in", 75),
        ("done.in", 70),
        ("noresearch.in", 65),
        ("small.in", 20),
        ("rejected.in", 60),
    ):
        insert_company(cid, cid)
        _score(profile.id, cid, score, {"employee": 0, "location": 40, "industry": 0})
    insert_memo(profile.id, "review.in")
    for cid in ("find.in", "write.in", "send.in", "done.in"):
        insert_memo(profile.id, cid, status="approved")
    rejected = insert_memo(profile.id, "rejected.in")
    set_review(rejected, "rejected")
    add_manual_contact("write.in", name="W")
    send_contact = add_manual_contact("send.in", name="S")
    done_contact = add_manual_contact("done.in", name="D")
    memos = {lead.company_id: lead.memo_id for lead in list_leads(profile)}
    _draft(memos["send.in"] or 0, send_contact.id, "approved")
    _draft(memos["done.in"] or 0, done_contact.id, "sent")

    leads = {lead.company_id: lead for lead in list_leads(profile)}
    assert [(c, leads[c].stage, leads[c].next_step) for c in leads] == [
        ("review.in", "review", "Review"),
        ("find.in", "approved", "Find contacts"),
        ("write.in", "approved", "Write email"),
        ("send.in", "approved", "Send email"),
        ("done.in", "contacted", "Contacted"),
        ("noresearch.in", "research", "Researched on the next search"),
        ("rejected.in", "notfit", "Skipped"),
        ("small.in", "notfit", "Skipped"),
    ]
    assert leads["rejected.in"].why_not == "You marked it not a fit"
    assert leads["small.in"].why_not.startswith("Smaller than your 51–2,000 range")
    assert stage_counts(list(leads.values()))["approved"] == 3
    assert review_queue(profile) == [memos["review.in"]]


def test_dashboard(profile: Profile) -> None:
    insert_company("a.in", "Alpha")  # based in Mumbai (the fixture's default city)
    _score(profile.id, "a.in", 90)
    insert_memo(profile.id, "a.in")
    board = dashboard(profile)
    assert (board.pipeline.found, board.pipeline.good_fit, board.pipeline.to_review) == (1, 1, 1)
    assert board.searched_locations == ["Mumbai"]
    assert board.unsearched_locations == ["Pune"]
    titles = [step.title for step in board.next_steps]
    assert titles[0] == "1 company is waiting for your review"
    assert titles[-1] == "1 of your 2 locations haven't been searched yet"


def test_fit_words() -> None:
    assert [fit_word(s, 50) for s in (100, 70, 55, 40, None)] == [
        "Great fit",
        "Great fit",
        "Good fit",
        "Weak fit",
        "Not scored",
    ]
