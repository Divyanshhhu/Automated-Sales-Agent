from src.citations import MEMO_LABELS, extract_cited_evidence_ids, strip_tags, validate_tagged_sentences

EMAIL_LABELS = frozenset({"PRODUCT", "INFERENCE"})


def test_email_labels_are_accepted_when_allowed() -> None:
    text = (
        "You launched two towers [E3]. Our agent replies instantly [PRODUCT]. "
        "That fits your launches [INFERENCE]."
    )
    assert validate_tagged_sentences(text, {3}, EMAIL_LABELS) == []


def test_label_not_allowed_here_is_flagged() -> None:
    issues = validate_tagged_sentences("Our agent replies instantly [PRODUCT].", set(), MEMO_LABELS)
    assert issues == ['Tag [PRODUCT] is not allowed here: "Our agent replies instantly [PRODUCT]."']


def test_untagged_and_unknown_evidence_are_flagged() -> None:
    issues = validate_tagged_sentences("You are growing. Big launch [E9].", {3}, EMAIL_LABELS)
    assert len(issues) == 2
    assert issues[0].startswith("Uncited claim")
    assert "E9" in issues[1]


def test_extract_cited_ids() -> None:
    assert extract_cited_evidence_ids("A [E1]. B [INFERENCE]. C [E12].") == {1, 12}


def test_strip_tags_repairs_spacing() -> None:
    text = "You launched 3 towers [E2]. We reply in seconds [PRODUCT].  It fits [INFERENCE]."
    assert strip_tags(text) == "You launched 3 towers. We reply in seconds. It fits."
