"""Rule-based Signal Confidence assignment.

Deliberately NOT an LLM judgment call -- confidence is derived from which
evidence categories were actually found, so it's auditable rather than a
second black box sitting on top of the memo generator.
"""

STRONG_CATEGORIES = {"direct_pain_point"}
PLAUSIBLE_CATEGORIES = {"expansion_launch", "hiring_leadership", "funding_financial", "tech_adoption"}

LABELS = {
    "strong": "\U0001F7E2 Strong signal",
    "plausible": "\U0001F7E1 Plausible fit",
    "weak": "\U0001F534 Weak/unclear",
}


def assign_confidence(evidence_items: list[dict]) -> str:
    categories = {e["category"] for e in evidence_items}
    if categories & STRONG_CATEGORIES:
        return LABELS["strong"]
    if categories & PLAUSIBLE_CATEGORIES:
        return LABELS["plausible"]
    return LABELS["weak"]
