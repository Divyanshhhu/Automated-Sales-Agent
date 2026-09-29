"""Deterministic citation checking for LLM-written text (memos and outreach
emails).

Every sentence must end with a tag saying where it came from: an evidence
id ([E12]) or an allowed label ([INFERENCE], [NO_EVIDENCE], [PRODUCT]).
Checking this after generation -- not just asking for it in the prompt -- is
what makes an unsupported claim visible instead of silently shipped.
"""
import re

# Every tag any caller may use; each caller decides which labels it allows.
TAG_RE = re.compile(r"\[(E(\d+)|INFERENCE|NO_EVIDENCE|PRODUCT)\]")
MEMO_LABELS = frozenset({"INFERENCE", "NO_EVIDENCE"})

# A bare sentence-boundary split on "." breaks abbreviations like "51 million
# sq. ft." into fragments, each missing the citation tag that only sits at
# the true sentence end -- a live batch flagged 4 false "uncited claim"
# issues on one fully-cited sentence for exactly this reason. Protect known
# abbreviations before splitting, restore them after.
_ABBREVIATIONS = ("sq.", "ft.", "rs.", "no.", "approx.", "etc.", "vs.", "e.g.", "i.e.", "mr.", "dr.")
_PLACEHOLDER_DOT = "․"  # one-dot leader: looks like "." but isn't a sentence end


def _protect_abbreviations(text: str) -> str:
    protected = text
    for abbr in _ABBREVIATIONS:
        replacement = abbr.replace(".", _PLACEHOLDER_DOT)
        protected = re.sub(re.escape(abbr), replacement, protected, flags=re.IGNORECASE)
    return protected


def sentences(text: str) -> list[str]:
    """Splits tagged text into sentences, keeping abbreviations like "sq. ft." whole."""
    return _sentences(text)


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", _protect_abbreviations(text.strip()))
    return [p.replace(_PLACEHOLDER_DOT, ".").strip() for p in parts if p.strip()]


def validate_tagged_sentences(
    text: str, valid_evidence_ids: set[int], allowed_labels: frozenset[str] = MEMO_LABELS
) -> list[str]:
    """Returns human-readable issues; an empty list means every sentence is
    tagged, every [E<id>] exists, and every label is allowed here.
    """
    issues = []
    for sentence in _sentences(text):
        tags = TAG_RE.findall(sentence)
        if not tags:
            issues.append(f'Uncited claim: "{sentence}"')
            continue
        for full_tag, e_num in tags:
            if e_num:
                if int(e_num) not in valid_evidence_ids:
                    issues.append(f'Citation references non-existent evidence id E{e_num}: "{sentence}"')
            elif full_tag not in allowed_labels:
                issues.append(f'Tag [{full_tag}] is not allowed here: "{sentence}"')
    return issues


def extract_cited_evidence_ids(text: str) -> set[int]:
    return {int(num) for _, num in TAG_RE.findall(text) if num}


def strip_tags(text: str) -> str:
    """The text a reader should see: tags removed, spacing repaired
    ("3 towers [E2]." -> "3 towers.").
    """
    without = re.sub(r"\s*" + TAG_RE.pattern, "", text)
    return re.sub(r"[ \t]{2,}", " ", without).strip()
