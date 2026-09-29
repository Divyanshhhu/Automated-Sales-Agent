import json

import pytest

from src import memo_generator
from src.confidence import LABELS, assign_confidence
from src.db import get_connection
from src.memo_generator import build_memo, validate_citations
from src.profiles import Profile

PRODUCT = {"name": "P", "description": "D", "problem_solved": "S", "differentiators": ["X"]}


def test_fully_cited_text_passes() -> None:
    text = "Launched 3 projects [E1]. This suggests lead volume is rising [INFERENCE]."
    assert validate_citations(text, {1}) == []


def test_uncited_sentence_flagged() -> None:
    issues = validate_citations("Launched 3 projects [E1]. They are growing fast.", {1})
    assert len(issues) == 1
    assert issues[0].startswith("Uncited claim")


def test_nonexistent_evidence_id_flagged() -> None:
    issues = validate_citations("Launched 3 projects [E9].", {1})
    assert len(issues) == 1
    assert "E9" in issues[0]


def test_abbreviations_do_not_split_sentences() -> None:
    # the live false positive: "sq. ft." split one cited sentence into 3 uncited fragments
    text = "The company delivered 51 million sq. ft. of space [E1]."
    assert validate_citations(text, {1}) == []


def test_no_evidence_tag_is_valid() -> None:
    assert validate_citations("The evidence does not show response delays [NO_EVIDENCE].", set()) == []


@pytest.mark.parametrize(
    ("categories", "expected"),
    [
        ({"direct_pain_point", "expansion_launch"}, LABELS["strong"]),
        ({"hiring_leadership"}, LABELS["plausible"]),
        (set(), LABELS["weak"]),
    ],
)
def test_assign_confidence(categories: set[str], expected: str) -> None:
    items = [{"id": i, "category": c, "fact_text": ""} for i, c in enumerate(sorted(categories))]
    assert assign_confidence(items) == expected


def test_build_memo_confidence_uses_only_cited_evidence(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_items = [
        {"id": 1, "category": "expansion_launch", "fact_text": "launch", "source_url": "u1"},
        {"id": 2, "category": "direct_pain_point", "fact_text": "complaint", "source_url": "u2"},
    ]
    # Pain-point evidence exists but the model didn't cite it -> Plausible, not Strong.
    monkeypatch.setattr(
        memo_generator,
        "_call_llm",
        lambda *_: {"why_relevant": "New launch [E1].", "potential_use_case": "Handle leads [INFERENCE]."},
    )
    memo = build_memo(PRODUCT, company_row, evidence_items, profile_id=profile.id, run_id=None)
    assert memo["signal_confidence"] == LABELS["plausible"]
    assert memo["issues"] == []

    conn = get_connection()
    stored = conn.execute("SELECT * FROM memos WHERE id=?", (memo["id"],)).fetchone()
    conn.close()
    assert stored["review_status"] == "pending"
    assert json.loads(stored["evidence_ids_json"]) == [1, 2]


def test_build_memo_with_citation_issues_needs_review(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        memo_generator,
        "_call_llm",
        lambda *_: {"why_relevant": "They are growing.", "potential_use_case": "Leads [E5]."},
    )
    memo = build_memo(PRODUCT, company_row, [], profile_id=profile.id, run_id=None)
    assert len(memo["issues"]) == 2
    assert memo["signal_confidence"] == LABELS["weak"]

    conn = get_connection()
    stored = conn.execute("SELECT review_status FROM memos WHERE id=?", (memo["id"],)).fetchone()
    conn.close()
    assert stored["review_status"] == "needs_review"
