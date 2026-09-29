import pytest

from src.confidence import LABELS, assess_signal

PHRASES = ["telecaller", "click-to-whatsapp", "pre-sales"]


def _e(i: int, category: str, text: str = "") -> dict:
    return {"id": i, "category": category, "fact_text": text}


def test_pain_point_is_strong() -> None:
    signal = assess_signal(
        [_e(1, "direct_pain_point", "No response from sales"), _e(2, "expansion_launch")], PHRASES
    )
    assert signal.label == LABELS["strong"]
    assert "E1" in signal.reason


def test_likely_need_phrase_in_cited_evidence() -> None:
    # the live Adani example
    fact = "Adani Realty reduced cost per lead by 30% using Click-to-WhatsApp Ads through AiSensy"
    signal = assess_signal([_e(15, "tech_adoption", fact)], PHRASES)
    assert signal.level == "likely"
    assert signal.label == LABELS["likely"]
    assert "E15 mentions “click-to-whatsapp”" in signal.reason


def test_phrases_match_whole_words_only() -> None:
    signal = assess_signal([_e(1, "hiring_leadership", "Hiring pre-salesforce admins")], PHRASES)
    assert signal.level == "plausible"


def test_general_signals_are_plausible_with_explanation() -> None:
    signal = assess_signal(
        [_e(1, "expansion_launch", "New tower"), _e(2, "funding_financial", "Record sales")], PHRASES
    )
    assert signal.level == "plausible"
    assert "expansion launch, funding financial" in signal.reason


def test_no_phrases_configured_means_no_likely_level() -> None:
    fact = "Hiring 20 telecallers for the new launch"
    assert assess_signal([_e(1, "hiring_leadership", fact)]).level == "plausible"
    assert assess_signal([_e(1, "hiring_leadership", fact)], PHRASES).level == "likely"


def test_nothing_cited_is_weak() -> None:
    assert assess_signal([], PHRASES).level == "weak"


@pytest.mark.parametrize(
    ("cited", "level"),
    [([_e(1, "expansion_launch")], "plausible"), ([], "weak")],
)
def test_uncited_pain_point_is_pointed_out(cited: list[dict], level: str) -> None:
    signal = assess_signal(cited, PHRASES, uncited_evidence=[_e(9, "direct_pain_point", "No reply")])
    assert signal.level == level  # the level only counts what's cited...
    assert "E9" in signal.reason and "regenerating" in signal.reason  # ...but the reason says what's missed
