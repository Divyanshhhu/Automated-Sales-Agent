import pytest

from src import memo_generator
from src.confidence import LABELS
from src.db import get_connection
from src.profiles import Profile, update_profile
from src.review import (
    MemoNotFoundError,
    get_memo,
    list_memos,
    next_memo_to_review,
    regenerate_memo,
    set_review,
    status_counts,
)
from tests.conftest import insert_company, insert_evidence, insert_memo


@pytest.fixture
def memos(profile: Profile) -> dict[str, int]:
    """Three memos under `profile` with different scores, signals and statuses."""
    insert_company("a.in", "Alpha")
    insert_company("b.in", "Beta")
    insert_company("c.in", "Gamma")
    return {
        "alpha": insert_memo(profile.id, "a.in", score=70, confidence=LABELS["strong"]),
        "beta": insert_memo(
            profile.id, "b.in", score=100, issues=["Uncited claim: x"], status="needs_review"
        ),
        "gamma": insert_memo(profile.id, "c.in", score=85, status="approved", confidence=LABELS["weak"]),
    }


def test_list_defaults_to_memos_awaiting_review_by_score(profile: Profile, memos: dict[str, int]) -> None:
    listed = list_memos(profile.id)
    assert [m.company_name for m in listed] == ["Beta", "Alpha"]
    assert listed[0].issue_count == 1


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"status": "all"}, ["Beta", "Gamma", "Alpha"]),
        ({"status": "approved"}, ["Gamma"]),
        ({"status": "all", "confidence": "strong"}, ["Alpha"]),
        ({"status": "all", "sort": "company"}, ["Alpha", "Beta", "Gamma"]),
        ({"status": "all", "sort": "confidence"}, ["Alpha", "Beta", "Gamma"]),
        # unknown values fall back to safe defaults instead of reaching SQL
        ({"status": "x; DROP TABLE memos", "sort": "id; --"}, ["Beta", "Alpha"]),
    ],
)
def test_list_filters_and_sorts(
    profile: Profile, memos: dict[str, int], kwargs: dict, expected: list[str]
) -> None:
    assert [m.company_name for m in list_memos(profile.id, **kwargs)] == expected


def test_status_counts(profile: Profile, memos: dict[str, int]) -> None:
    assert status_counts(profile.id) == {"to_review": 2, "approved": 1, "rejected": 0, "all": 3}


def test_memo_detail_marks_cited_evidence_and_hides_evidence_not_shown(profile: Profile) -> None:
    insert_company("a.in", "Alpha")
    cited = insert_evidence(profile.id, "a.in", url="https://news/1")
    uncited = insert_evidence(profile.id, "a.in", url="https://news/2")
    later = insert_evidence(profile.id, "a.in", url="https://news/3")  # fetched after the memo was written
    memo_id = insert_memo(
        profile.id, "a.in", why=f"New tower [E{cited}].", evidence_ids=[cited, uncited]
    )
    memo = get_memo(memo_id)
    assert memo.profile_name == "Test"
    assert memo.company["name"] == "Alpha"
    assert {e["id"]: e["cited"] for e in memo.evidence} == {cited: True, uncited: False}
    assert later not in memo.evidence_ids


def test_approve_reject_and_reset(profile: Profile, memos: dict[str, int]) -> None:
    beta = memos["beta"]
    assert set_review(beta, "approved", "  looks good  ") == "approved"
    memo = get_memo(beta)
    assert (memo.review_status, memo.review_notes) == ("approved", "looks good")
    assert memo.reviewed_at is not None

    # reset restores the automatic status: this memo has citation issues
    assert set_review(beta, "reset", memo.review_notes or "") == "needs_review"
    assert get_memo(beta).reviewed_at is None
    assert set_review(memos["alpha"], "rejected") == "rejected"
    assert get_memo(memos["alpha"]).review_notes is None


def test_invalid_decision_rejected(profile: Profile, memos: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        set_review(memos["alpha"], "maybe")


def test_notes_are_capped(profile: Profile, memos: dict[str, int]) -> None:
    set_review(memos["alpha"], "approved", "x" * 5000)
    notes = get_memo(memos["alpha"]).review_notes
    assert notes is not None and len(notes) == 2000


def test_next_memo_to_review(profile: Profile, memos: dict[str, int]) -> None:
    assert next_memo_to_review(profile.id) == memos["beta"]
    assert next_memo_to_review(profile.id, exclude_id=memos["beta"]) == memos["alpha"]
    set_review(memos["alpha"], "approved")
    set_review(memos["beta"], "rejected")
    assert next_memo_to_review(profile.id) is None


def test_missing_memo(profile: Profile) -> None:
    with pytest.raises(MemoNotFoundError):
        get_memo(999)


def test_regenerate_rewrites_from_stored_evidence_with_current_product(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_id = insert_evidence(profile.id, "c1", category="direct_pain_point", fact="Buyers wait days")
    memo_id = insert_memo(profile.id, "c1", why="Old text.", issues=["Uncited claim: Old text."],
                          status="needs_review", evidence_ids=[evidence_id])
    set_review(memo_id, "rejected", "too vague")
    new_product = {**profile.config["product"], "name": "Renamed Product"}
    update_profile(profile.id, config={**profile.config, "product": new_product})

    seen: dict = {}

    def fake_llm(product: dict, company_row: object, evidence_items: list[dict]) -> dict:
        seen["product"], seen["evidence"] = product["name"], [e["id"] for e in evidence_items]
        return {"why_relevant": f"Buyers wait [E{evidence_id}].", "potential_use_case": "Fix it [INFERENCE]."}

    monkeypatch.setattr(memo_generator, "_call_llm", fake_llm)
    memo = regenerate_memo(memo_id)

    assert seen == {"product": "Renamed Product", "evidence": [evidence_id]}
    assert memo.why_relevant == f"Buyers wait [E{evidence_id}]."
    assert memo.issues == []
    assert memo.signal_confidence == LABELS["strong"]
    assert memo.review_status == "pending"  # the old decision judged text that no longer exists
    assert memo.reviewed_at is None
    assert memo.review_notes == "too vague"  # notes say what was wrong: keep them


def test_regenerate_failure_leaves_memo_unchanged(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    memo_id = insert_memo(profile.id, "c1", why="Original [INFERENCE].")

    def broken_llm(*_: object) -> dict:
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(memo_generator, "_call_llm", broken_llm)
    with pytest.raises(RuntimeError):
        regenerate_memo(memo_id)
    conn = get_connection()
    text = conn.execute("SELECT why_relevant_text FROM memos WHERE id=?", (memo_id,)).fetchone()[0]
    conn.close()
    assert text == "Original [INFERENCE]."
