"""The one real LLM-judgment step in the pipeline: synthesizing a grounded
research memo from retrieved evidence.

Grounding is enforced structurally, not just by prompting: every claim must
cite a real evidence ID or be explicitly tagged [INFERENCE]. The
citation_validator below checks this deterministically after generation --
that check, not the prompt wording, is what makes hallucination structurally
hard rather than merely discouraged.
"""
import json
import os
import re
from dataclasses import dataclass

from openai import (
    APIConnectionError,
    APITimeoutError,
    InternalServerError,
    OpenAI,
    RateLimitError,
)

from .confidence import assign_confidence
from .db import get_connection, utc_now
from .retry import call_with_retry

MODEL = os.environ.get("OPENAI_MODEL", "gpt-6-luna")

MEMO_SCHEMA = {
    "type": "object",
    "properties": {
        "why_relevant": {"type": "string"},
        "potential_use_case": {"type": "string"},
    },
    "required": ["why_relevant", "potential_use_case"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are a B2B sales research analyst. You will be given a product \
profile and a company, along with a numbered list of EVIDENCE items retrieved about \
that company (each with an ID like E1, E2).

Write a short research memo with two fields: "why_relevant" and "potential_use_case".

Hard rules:
- Every factual claim about the company MUST end with a citation tag referencing the \
evidence ID it came from, e.g. "...launched 3 new projects this year [E2]."
- If you want to state a reasoned connection between the evidence and the product's \
relevance that is NOT directly stated in any evidence item, you MUST tag that sentence \
[INFERENCE] instead of an evidence ID. Do not blend a fact and an inference in the same \
sentence without both tags.
- If you need to explicitly note that the evidence does NOT establish something (an \
honest gap acknowledgment, e.g. "the evidence does not show response delays"), tag that \
sentence [NO_EVIDENCE]. Every sentence must end in exactly one of: an [E<id>] citation, \
[INFERENCE], or [NO_EVIDENCE].
- Do NOT invent facts, numbers, dates, or people not present in the evidence. If there \
is not enough evidence to say anything specific, say so plainly.
- Do NOT name or guess at a specific decision-maker's name. Only refer to roles.
- Output ONLY valid JSON: {"why_relevant": "...", "potential_use_case": "..."}
"""


def _format_evidence(evidence_items: list[dict]) -> str:
    lines = []
    for e in evidence_items:
        lines.append(f"[E{e['id']}] ({e['category']}) {e['fact_text']} (source: {e['source_url']})")
    return "\n".join(lines) if lines else "(no evidence retrieved)"


def _call_llm(product: dict, company_row, evidence_items: list[dict]) -> dict:
    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
    user_content = f"""PRODUCT:
Name: {product['name']}
Description: {product['description']}
Problem solved: {product['problem_solved']}
Differentiators: {', '.join(product['differentiators'])}

COMPANY:
Name: {company_row['name']}
Industry: {company_row['industry']}
Location: {', '.join([v for v in [company_row['city'], company_row['state'], company_row['country']] if v])}
Employee count: {company_row['employee_count']}
Description: {company_row['short_description']}

EVIDENCE:
{_format_evidence(evidence_items)}
"""

    def _do_call():
        return client.responses.create(
            model=MODEL,
            instructions=SYSTEM_PROMPT,
            input=user_content,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "research_memo",
                    "strict": True,
                    "schema": MEMO_SCHEMA,
                }
            },
        )

    response = call_with_retry(
        _do_call,
        is_retriable=_is_retriable_openai_error,
        context=f"OpenAI memo generation for {company_row['name']!r}",
    )
    return json.loads(response.output_text)


def _is_retriable_openai_error(exc: Exception) -> bool:
    return isinstance(exc, (APITimeoutError, APIConnectionError, RateLimitError, InternalServerError))


CITATION_TAG_RE = re.compile(r"\[(E(\d+)|INFERENCE|NO_EVIDENCE)\]")

# A bare sentence-boundary split on "." breaks abbreviations like "51 million
# sq. ft." into fragments, each missing the citation tag that only sits at
# the true sentence end -- a live batch flagged 4 false "uncited claim"
# issues on one fully-cited sentence for exactly this reason. Protect known
# abbreviations before splitting, restore them after.
_ABBREVIATIONS = ("sq.", "ft.", "rs.", "no.", "approx.", "etc.", "vs.", "e.g.", "i.e.", "mr.", "dr.")


def _protect_abbreviations(text: str) -> str:
    protected = text
    for abbr in _ABBREVIATIONS:
        protected = re.sub(re.escape(abbr), abbr.replace(".", "․"), protected, flags=re.IGNORECASE)
    return protected


def _restore_abbreviations(text: str) -> str:
    return text.replace("․", ".")


def validate_citations(text: str, valid_evidence_ids: set[int]) -> list[str]:
    """Deterministic check: every sentence must carry a valid citation tag.

    Returns a list of human-readable issues; empty list means the memo passed.
    """
    issues = []
    sentences = [
        _restore_abbreviations(s)
        for s in re.split(r"(?<=[.!?])\s+", _protect_abbreviations(text.strip()))
    ]
    for sentence in sentences:
        if not sentence.strip():
            continue
        tags = CITATION_TAG_RE.findall(sentence)
        if not tags:
            issues.append(f"Uncited claim: \"{sentence.strip()}\"")
            continue
        for _full_tag, e_num in tags:
            if e_num and int(e_num) not in valid_evidence_ids:
                issues.append(
                    f"Citation references non-existent evidence id E{e_num}: \"{sentence.strip()}\""
                )
    return issues


def extract_cited_evidence_ids(text: str) -> set[int]:
    return {int(num) for _, num in CITATION_TAG_RE.findall(text) if num}


@dataclass(frozen=True)
class MemoContent:
    """A generated, citation-checked memo -- not yet stored."""

    why_relevant: str
    potential_use_case: str
    issues: list[str]
    signal_confidence: str
    evidence_ids: list[int]  # every evidence item the model was shown

    @property
    def review_status(self) -> str:
        return "needs_review" if self.issues else "pending"


def write_memo(product: dict, company_row, evidence_items: list[dict]) -> MemoContent:
    """Calls the model, then checks every citation deterministically."""
    valid_ids = {e["id"] for e in evidence_items}
    result = _call_llm(product, company_row, evidence_items)

    texts = {field: result.get(field) or "" for field in ("why_relevant", "potential_use_case")}
    all_issues: list[str] = []
    cited_ids: set[int] = set()
    for text in texts.values():
        all_issues.extend(validate_citations(text, valid_ids))
        cited_ids |= extract_cited_evidence_ids(text)

    # Confidence reflects what the model actually cited as meaningful, not
    # merely which category searches happened to return some result --
    # a Tavily query returns *something* for almost any input, so raw
    # category presence is not proof of a real signal.
    cited_evidence = [e for e in evidence_items if e["id"] in cited_ids]
    return MemoContent(
        why_relevant=texts["why_relevant"],
        potential_use_case=texts["potential_use_case"],
        issues=all_issues,
        signal_confidence=assign_confidence(cited_evidence),
        evidence_ids=sorted(valid_ids),
    )


def build_memo(
    product: dict, company_row, evidence_items: list[dict], *, profile_id: int, run_id: int | None
) -> dict:
    """Writes a memo and stores it. company_row must carry the profile's
    icp_fit_score / icp_fit_label (the pipeline joins them in from
    company_scores); they're snapshotted onto the memo.
    """
    memo = write_memo(product, company_row, evidence_items)

    conn = get_connection()
    cursor = conn.execute(
        """
        INSERT INTO memos (
            profile_id, company_id, run_id, icp_fit_score, icp_fit_label, signal_confidence,
            why_relevant_text, potential_use_case_text, evidence_ids_json,
            citation_issues, review_status, generated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            profile_id,
            company_row["id"],
            run_id,
            company_row["icp_fit_score"],
            company_row["icp_fit_label"],
            memo.signal_confidence,
            memo.why_relevant,
            memo.potential_use_case,
            json.dumps(memo.evidence_ids),
            json.dumps(memo.issues) if memo.issues else None,
            memo.review_status,
            utc_now(),
        ),
    )
    conn.commit()
    memo_id = cursor.lastrowid
    conn.close()
    return {
        "id": memo_id,
        "issues": memo.issues,
        "signal_confidence": memo.signal_confidence,
        "why_relevant": memo.why_relevant,
        "potential_use_case": memo.potential_use_case,
    }
