"""Rule-based Signal Confidence: is there evidence the company needs the
product *now*?

Deliberately NOT an LLM judgment call -- it's derived from which evidence
the memo actually cited, so it's auditable rather than a second black box on
top of the memo generator. It answers a different question from ICP Fit
("is this the right kind of company?"); the two must never be collapsed.

Levels, strongest first:
- Strong:      cites direct evidence of the problem (a pain-point complaint).
- Likely need: no complaint, but cites evidence containing one of the
               profile's "likely need" phrases -- a strong indirect sign of
               the same problem (e.g. hiring telecallers, running
               click-to-WhatsApp lead ads).
- Plausible:   cites only general signals (launches, hiring, funding, tech).
- Weak:        cites nothing specific.
"""
import re
from dataclasses import dataclass

STRONG_CATEGORIES = {"direct_pain_point"}

LABELS = {
    "strong": "\U0001F7E2 Strong signal",
    "likely": "\U0001F535 Likely need",
    "plausible": "\U0001F7E1 Plausible fit",
    "weak": "\U0001F534 Weak/unclear",
}
# Strongest first -- used for sorting everywhere.
LEVEL_ORDER = ("strong", "likely", "plausible", "weak")
# What the UI calls each level. LABELS stay as stored (and in CSV exports) so
# existing data keeps working; only the words people read changed.
DISPLAY_NAMES = {
    "strong": "Clear need",
    "likely": "Likely need",
    "plausible": "Possible need",
    "weak": "No sign yet",
}


def level_of(label: str | None) -> str:
    """The level key for a stored label; unknown or missing reads as weak."""
    for level, stored in LABELS.items():
        if stored == label:
            return level
    return "weak"


_CATEGORY_NAMES = {
    "direct_pain_point": "customer complaints",
    "expansion_launch": "new launches",
    "hiring_leadership": "hiring",
    "funding_financial": "sales and funding",
    "tech_adoption": "tech and tools",
}


def category_label(category: str) -> str:
    """Plain-language name of a signal category for the UI."""
    return _CATEGORY_NAMES.get(category, category.replace("_", " "))


@dataclass(frozen=True)
class SignalAssessment:
    level: str  # a LABELS key
    reason: str  # plain-language explanation shown to the reviewer
    summary: str = ""  # a few words for list rows

    @property
    def name(self) -> str:
        return DISPLAY_NAMES[self.level]

    @property
    def label(self) -> str:
        return LABELS[self.level]


def _phrase_hits(text: str, phrases: list[str]) -> list[str]:
    """Whole-word matches, allowing a plural ("telecaller" finds "telecallers",
    but "pre-sales" doesn't find "pre-salesforce").
    """
    lowered = text.lower()
    return [
        p
        for p in phrases
        if p.strip() and re.search(rf"(?<!\w){re.escape(p.strip().lower())}(?:s|es)?(?!\w)", lowered)
    ]


def _ids(items: list[dict]) -> str:
    return ", ".join(f"E{e['id']}" for e in items)


def assess_signal(
    cited_evidence: list[dict],
    likely_need_phrases: list[str] | None = None,
    uncited_evidence: list[dict] | None = None,
) -> SignalAssessment:
    """Decides the signal level from the evidence the memo cited, and says
    why. `uncited_evidence` (retrieved but not cited) only feeds the
    explanation: a stronger item the memo skipped is worth pointing out.
    """
    phrases = likely_need_phrases or []
    uncited = uncited_evidence or []

    pain = [e for e in cited_evidence if e["category"] in STRONG_CATEGORIES]
    if pain:
        return SignalAssessment(
            "strong",
            f"Cites direct evidence of the problem: {_ids(pain)}.",
            "Customers complain about slow or missing replies",
        )

    skipped_pain = [e for e in uncited if e["category"] in STRONG_CATEGORIES]
    hint = (
        f" Pain-point evidence was found ({_ids(skipped_pain)}) but the memo doesn't cite it"
        " -- regenerating the memo may lift this to Strong."
        if skipped_pain
        else ""
    )

    likely = [(e, hits) for e in cited_evidence if (hits := _phrase_hits(e.get("fact_text") or "", phrases))]
    if likely:
        details = "; ".join(f"E{e['id']} mentions “{hits[0]}”" for e, hits in likely)
        return SignalAssessment(
            "likely",
            f"No direct complaints cited, but a strong sign of the need: {details}.{hint}",
            f"Mentions “{likely[0][1][0]}”",
        )

    if cited_evidence:
        categories = sorted({category_label(e["category"]) for e in cited_evidence})
        return SignalAssessment(
            "plausible",
            f"Cites general signals only ({', '.join(categories)}); no complaints about responsiveness"
            f" and none of the profile's likely-need phrases in the cited evidence.{hint}",
            "General signs: " + ", ".join(categories),
        )
    return SignalAssessment("weak", f"The memo cites no specific evidence.{hint}", "Nothing specific found")


def assign_confidence(evidence_items: list[dict], likely_need_phrases: list[str] | None = None) -> str:
    """The label for a set of cited evidence items."""
    return assess_signal(evidence_items, likely_need_phrases).label
