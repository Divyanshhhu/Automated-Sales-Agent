"""Outreach email drafts: written from an approved memo to one named person.

The model writes only the middle of the email, and every sentence must be
tagged: [E<id>] for a fact about the company (from evidence the approved
memo cited), [PRODUCT] for a claim about the product, [INFERENCE] for the
connection between them. The same deterministic checker used for memos
verifies that; the tags are then stripped for the version you send.

The greeting, call-to-action link and signature are added by code, not the
model, so the link and its tracking reference can't be mangled. Nothing is
ever sent automatically: a person approves, sends it themselves, and marks
it sent.
"""
import json
import secrets
import sqlite3
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .citations import strip_tags, validate_tagged_sentences
from .contacts import Contact, get_contact
from .db import get_connection, utc_now
from .llm import call_structured
from .profiles import get_profile
from .review import get_memo

EMAIL_LABELS = frozenset({"PRODUCT", "INFERENCE"})
MAX_SUBJECT_LENGTH = 150
MAX_BODY_LENGTH = 5000
_WHATSAPP_HOSTS = ("wa.me", "api.whatsapp.com")

EMAIL_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["subject", "body"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You write short, specific, respectful B2B cold emails.

You get: the PRODUCT being offered, the RECIPIENT (name, title, company), an \
approved research MEMO on why their company is a fit, and numbered EVIDENCE items \
(E1, E2...) about the company.

Write two fields:
- "subject": at most 8 words. Plain text, no tags, no emoji, no clickbait.
- "body": 3 to 5 sentences. No greeting, no sign-off, no link and no call to action \
(those are added separately). Do not mention research, AI, or that you looked them up.

Hard rules for "body":
- Every sentence MUST end with exactly one tag, placed before the final punctuation:
  [E<id>] if it states a fact about the company -- only facts from the EVIDENCE listed;
  [PRODUCT] if it describes the product -- only what PRODUCT says;
  [INFERENCE] if it connects the two (e.g. why the fact makes the product relevant).
- Never invent facts, numbers, dates, names, customers or results.
- Speak to the recipient's role; keep it concise and specific.

Output ONLY valid JSON: {"subject": "...", "body": "..."}
"""


class OutreachError(ValueError):
    """A draft action that isn't allowed in the current state."""


class DraftNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Draft:
    id: int
    memo_id: int
    contact_id: int
    profile_id: int
    company_name: str
    contact_name: str
    contact_title: str | None
    contact_email: str | None
    ref_code: str
    generated_body: str
    citation_issues: list[str]
    subject: str
    body: str
    status: str
    created_at: str
    updated_at: str
    approved_at: str | None
    sent_at: str | None


# ---------- building the email ----------


def tracked_link(url: str, ref_code: str) -> str:
    """Adds the draft's reference code to the call-to-action link, so a
    reply or booking can be traced back to this email: in the pre-filled
    message for WhatsApp links, as utm parameters otherwise (Calendly and
    most booking tools record them).
    """
    parts = urlparse(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if parts.netloc.lower() in _WHATSAPP_HOSTS:
        text = query.get("text", "").strip() or "Hi, I'd like to see how it works"
        query["text"] = f"{text} (ref {ref_code})"
    else:
        query.update({"utm_source": "sales-agent", "utm_medium": "email", "utm_content": ref_code})
    return urlunparse(parts._replace(query=urlencode(query)))


def compose_email(first_name: str, body: str, outreach: dict, ref_code: str) -> str:
    greeting = f"Hi {first_name}," if first_name else "Hello,"
    url = (outreach.get("cta_url") or "").strip()
    if url:
        label = (outreach.get("cta_label") or "").strip() or "You can pick a time that suits you here"
        call_to_action = f"{label}: {tracked_link(url, ref_code)}"
    else:
        call_to_action = "Would you be open to a 15-minute call next week? Just reply to this email."
    sign_off = (outreach.get("signature") or "").strip() or (outreach.get("sender_name") or "").strip()
    return "\n\n".join(part for part in (greeting, body, call_to_action, sign_off) if part)


def _format_evidence(evidence: list[dict]) -> str:
    lines = [f"[E{e['id']}] {e['fact_text']}" for e in evidence]
    return "\n".join(lines) or "(none -- use [PRODUCT] and [INFERENCE] only)"


def _call_llm(
    product: dict, contact: Contact, company_name: str, memo_text: str, evidence: list[dict]
) -> dict:
    user_content = f"""PRODUCT:
Name: {product['name']}
Description: {product['description']}
Problem solved: {product['problem_solved']}
Differentiators: {', '.join(product['differentiators'])}

RECIPIENT:
Name: {contact.name}
Title: {contact.title or 'unknown'}
Company: {company_name}

MEMO:
{memo_text}

EVIDENCE:
{_format_evidence(evidence)}
"""
    return call_structured(
        instructions=SYSTEM_PROMPT,
        user_content=user_content,
        schema=EMAIL_SCHEMA,
        schema_name="outreach_email",
        context=f"OpenAI outreach email for contact {contact.id}",
    )


def _new_ref_code(conn: sqlite3.Connection) -> str:
    while True:
        code = "SA-" + secrets.token_hex(3).upper()
        if not conn.execute("SELECT 1 FROM outreach_drafts WHERE ref_code=?", (code,)).fetchone():
            return code


def generate_draft(memo_id: int, contact_id: int) -> Draft:
    """Writes (or rewrites) the email to one person from one approved memo.
    Costs one LLM call. A draft already marked sent is never overwritten.
    """
    memo = get_memo(memo_id)
    if memo.review_status != "approved":
        raise OutreachError("Approve the memo before writing an email from it")
    contact = get_contact(contact_id)
    if contact.company_id != memo.company["id"]:
        raise OutreachError("That person doesn't work at this memo's company")
    existing = _find_draft(memo_id, contact_id)
    if existing and existing.status == "sent":
        raise OutreachError("This email was already sent; it can't be rewritten")

    config = get_profile(memo.profile_id).config
    outreach = config.get("outreach", {})
    cited = [e for e in memo.evidence if e["cited"]]
    memo_text = f"{memo.why_relevant}\n{memo.potential_use_case}"
    result = _call_llm(config["product"], contact, memo.company["name"], memo_text, cited)

    generated_body = (result.get("body") or "").strip()
    raw_subject = (result.get("subject") or "").strip()
    issues = validate_tagged_sentences(generated_body, {e["id"] for e in cited}, EMAIL_LABELS)
    if strip_tags(raw_subject) != raw_subject:
        issues.append(f'Subject contained citation tags (removed): "{raw_subject}"')
    fallback_subject = f"{memo.company['name']} and {config['product']['name']}"
    subject = strip_tags(raw_subject)[:MAX_SUBJECT_LENGTH] or fallback_subject

    now = utc_now()
    conn = get_connection()
    try:
        ref_code = existing.ref_code if existing else _new_ref_code(conn)
        body = compose_email(contact.first_name, strip_tags(generated_body), outreach, ref_code)
        conn.execute(
            """
            INSERT INTO outreach_drafts
                (memo_id, contact_id, ref_code, generated_body, citation_issues, subject, body,
                 status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            ON CONFLICT (memo_id, contact_id) DO UPDATE SET
                generated_body=excluded.generated_body, citation_issues=excluded.citation_issues,
                subject=excluded.subject, body=excluded.body, status='draft',
                updated_at=excluded.updated_at, approved_at=NULL
            """,
            (memo_id, contact_id, ref_code, generated_body, json.dumps(issues) if issues else None,
             subject, body, now, now),
        )
        conn.commit()
    finally:
        conn.close()
    draft = _find_draft(memo_id, contact_id)
    assert draft is not None
    return draft


# ---------- reading ----------

_SELECT_DRAFTS = """
    SELECT d.*, m.profile_id, c.name AS contact_name, c.title AS contact_title, c.email AS contact_email,
           co.name AS company_name
    FROM outreach_drafts d
    JOIN memos m ON m.id = d.memo_id
    JOIN contacts c ON c.id = d.contact_id
    JOIN companies co ON co.id = c.company_id
"""


def _from_row(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        memo_id=row["memo_id"],
        contact_id=row["contact_id"],
        profile_id=row["profile_id"],
        company_name=row["company_name"],
        contact_name=row["contact_name"],
        contact_title=row["contact_title"],
        contact_email=row["contact_email"],
        ref_code=row["ref_code"],
        generated_body=row["generated_body"],
        citation_issues=json.loads(row["citation_issues"]) if row["citation_issues"] else [],
        subject=row["subject"],
        body=row["body"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        approved_at=row["approved_at"],
        sent_at=row["sent_at"],
    )


def _find_draft(memo_id: int, contact_id: int) -> Draft | None:
    conn = get_connection()
    try:
        row = conn.execute(
            f"{_SELECT_DRAFTS} WHERE d.memo_id=? AND d.contact_id=?", (memo_id, contact_id)
        ).fetchone()
    finally:
        conn.close()
    return _from_row(row) if row else None


def get_draft(draft_id: int) -> Draft:
    conn = get_connection()
    try:
        row = conn.execute(f"{_SELECT_DRAFTS} WHERE d.id=?", (draft_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise DraftNotFoundError(f"No email draft with id {draft_id}")
    return _from_row(row)


def list_drafts(profile_id: int | None = None, status: str | None = None) -> list[Draft]:
    where: list[str] = []
    params: list[object] = []
    if profile_id is not None:
        where.append("m.profile_id = ?")
        params.append(profile_id)
    if status in ("draft", "approved", "sent"):
        where.append("d.status = ?")
        params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = get_connection()
    try:
        rows = conn.execute(
            f"{_SELECT_DRAFTS} {clause} ORDER BY d.updated_at DESC, d.id DESC", params
        ).fetchall()
    finally:
        conn.close()
    return [_from_row(r) for r in rows]


def drafts_for_memo(memo_id: int) -> dict[int, Draft]:
    """contact_id -> draft, for showing each person's email status on a memo."""
    conn = get_connection()
    try:
        rows = conn.execute(f"{_SELECT_DRAFTS} WHERE d.memo_id=?", (memo_id,)).fetchall()
    finally:
        conn.close()
    return {r["contact_id"]: _from_row(r) for r in rows}


# ---------- changing ----------


def update_draft(draft_id: int, *, subject: str, body: str) -> Draft:
    """Saves your edits. Editing an approved draft sends it back to "draft":
    the approval was for the old text.
    """
    draft = get_draft(draft_id)
    if draft.status == "sent":
        raise OutreachError("This email was already sent; it can't be edited")
    subject, body = subject.strip(), body.strip()
    if not subject or not body:
        raise OutreachError("Subject and body can't be empty")
    if len(subject) > MAX_SUBJECT_LENGTH or len(body) > MAX_BODY_LENGTH:
        raise OutreachError("Subject or body is too long")
    _set(draft_id, "subject=?, body=?, status='draft', approved_at=NULL", (subject, body))
    return get_draft(draft_id)


def approve_draft(draft_id: int) -> Draft:
    draft = get_draft(draft_id)
    if draft.status != "draft":
        raise OutreachError(f"Only a draft can be approved (this one is {draft.status})")
    _set(draft_id, "status='approved', approved_at=?", (utc_now(),))
    return get_draft(draft_id)


def mark_sent(draft_id: int) -> Draft:
    draft = get_draft(draft_id)
    if draft.status != "approved":
        raise OutreachError("Approve the email before marking it as sent")
    _set(draft_id, "status='sent', sent_at=?", (utc_now(),))
    return get_draft(draft_id)


def _set(draft_id: int, sql_set: str, params: tuple) -> None:
    conn = get_connection()
    try:
        conn.execute(
            f"UPDATE outreach_drafts SET {sql_set}, updated_at=? WHERE id=?", (*params, utc_now(), draft_id)
        )
        conn.commit()
    finally:
        conn.close()
