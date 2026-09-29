"""The people to contact at a company: found with Exa people search, or
added by hand.

These are real people's personal details (India's DPDP Act applies), so
the module stores as little as it can: only work details, only people who
*currently* work at the company in a role the profile targets, at most a
handful per search, each tagged with where it came from -- and any contact
can be deleted (which also deletes the drafts addressed to them).
"""
import re
import sqlite3
from dataclasses import dataclass

import requests

from .db import get_connection, utc_now
from .exa_client import exa_search

MAX_CONTACTS_PER_SEARCH = 5
PEOPLE_RESULTS_PER_SEARCH = 10
MAX_FIELD_LENGTH = 200
DEFAULT_ROLE_WORDS = frozenset({"sales", "crm", "founder"})

# Words in a role like "Head of Sales" that say how senior, not which function.
_ROLE_STOPWORDS = frozenset({
    "head", "of", "vp", "vice", "president", "director", "chief", "officer", "senior", "the", "and",
    "manager", "lead", "general", "assistant", "associate", "deputy", "executive", "&",
})
_SENIOR_RE = re.compile(
    r"\b(head|vp|avp|evp|svp|vice president|director|chief|ceo|coo|cso|cro|cxo|president|"
    r"co-?founder|founder|general manager|gm|managing director|md|partner|owner)\b",
    re.IGNORECASE,
)
# Titles that are the decision-maker whatever the profile's role words say.
_TOP_DECISION_RE = re.compile(r"\b(co-?founder|founder|ceo|managing director|owner)\b", re.IGNORECASE)
_LEGAL_SUFFIXES = frozenset(
    {"limited", "ltd", "pvt", "private", "llp", "inc", "corp", "corporation", "co", "company", "the"}
)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ContactNotFoundError(LookupError):
    pass


class ContactValidationError(ValueError):
    pass


@dataclass(frozen=True)
class Contact:
    id: int
    company_id: str
    name: str
    title: str | None
    location: str | None
    profile_url: str | None
    email: str | None
    source: str
    created_at: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0] if self.name.split() else ""


@dataclass(frozen=True)
class Candidate:
    name: str
    title: str
    location: str | None
    profile_url: str | None
    rank: int  # 0: senior decision-maker in a target role; 1: in a target role


@dataclass(frozen=True)
class FindResult:
    added: list[Contact]
    already_saved: int  # matching people who were already stored for this company
    results_checked: int


# ---------- matching ----------


def role_words(roles: list[str]) -> frozenset[str]:
    """Function words from the profile's roles: "Head of Sales" -> "sales"."""
    words = {
        w for role in roles for w in re.findall(r"[a-z][a-z-]*", role.lower()) if w not in _ROLE_STOPWORDS
    }
    return frozenset(words) or DEFAULT_ROLE_WORDS


def _normalize_company(name: str) -> str:
    words = re.findall(r"[a-z0-9]+", name.lower())
    return " ".join(w for w in words if w not in _LEGAL_SUFFIXES)


def same_company(a: str, b: str) -> bool:
    """"Tata Housing Development Company Limited" matches "Tata Housing";
    "Kumar World" does not match "Kumar Properties" (a prefix must end on a
    word boundary, and only legal suffixes are ignored).
    """
    x, y = _normalize_company(a), _normalize_company(b)
    if not x or not y:
        return False
    short, long = sorted((x, y), key=len)
    return long == short or long.startswith(short + " ")


def title_rank(title: str, words: frozenset[str]) -> int | None:
    """0 = senior person in a target function (or founder/CEO), 1 = in a
    target function, None = not someone to contact for this profile.
    """
    in_function = any(re.search(rf"\b{re.escape(w)}\b", title, re.IGNORECASE) for w in words)
    if _TOP_DECISION_RE.search(title) or (in_function and _SENIOR_RE.search(title)):
        return 0
    return 1 if in_function else None


def candidate_from_result(result: dict, company_name: str, words: frozenset[str]) -> Candidate | None:
    """A person currently working at `company_name` in a target role, or None."""
    person = result.get("entity")
    if not isinstance(person, dict):
        return None
    name = person.get("name") or " ".join(
        p for p in (person.get("firstName"), person.get("lastName")) if isinstance(p, str)
    )
    if not isinstance(name, str) or not name.strip():
        return None
    best: tuple[int, str] | None = None
    for job in person.get("workHistory") or []:
        if not isinstance(job, dict) or (job.get("dates") or {}).get("to"):
            continue  # a past job
        company = (job.get("company") or {}).get("name")
        title = job.get("title")
        if not isinstance(company, str) or not isinstance(title, str):
            continue
        if not same_company(company, company_name):
            continue
        rank = title_rank(title, words)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, title.strip())
    if best is None:
        return None
    url = result.get("url")
    location = person.get("location")
    return Candidate(
        name=name.strip()[:MAX_FIELD_LENGTH],
        title=best[1][:MAX_FIELD_LENGTH],
        location=location[:MAX_FIELD_LENGTH] if isinstance(location, str) else None,
        profile_url=url if isinstance(url, str) and url.startswith(("https://", "http://")) else None,
        rank=best[0],
    )


# ---------- storage ----------


def _from_row(row: sqlite3.Row) -> Contact:
    return Contact(
        id=row["id"],
        company_id=row["company_id"],
        name=row["name"],
        title=row["title"],
        location=row["location"],
        profile_url=row["profile_url"],
        email=row["email"],
        source=row["source"],
        created_at=row["created_at"],
    )


def list_contacts(company_id: str) -> list[Contact]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM contacts WHERE company_id=? ORDER BY id", (company_id,)).fetchall()
    finally:
        conn.close()
    return [_from_row(r) for r in rows]


def get_contact(contact_id: int) -> Contact:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM contacts WHERE id=?", (contact_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ContactNotFoundError(f"No contact with id {contact_id}")
    return _from_row(row)


def _clean(
    name: str, title: str, email: str, profile_url: str
) -> tuple[str, str | None, str | None, str | None]:
    name, title, email, profile_url = (v.strip() for v in (name, title, email, profile_url))
    if not name:
        raise ContactValidationError("Name is required")
    if any(len(v) > MAX_FIELD_LENGTH for v in (name, title, email)) or len(profile_url) > 500:
        raise ContactValidationError("One of the fields is too long")
    if email and not _EMAIL_RE.match(email):
        raise ContactValidationError(f"{email!r} doesn't look like an email address")
    if profile_url and not profile_url.startswith(("https://", "http://")):
        raise ContactValidationError("Profile link must start with https://")
    return name, title or None, email or None, profile_url or None


def add_manual_contact(
    company_id: str, *, name: str, title: str = "", email: str = "", profile_url: str = ""
) -> Contact:
    name, clean_title, clean_email, clean_url = _clean(name, title, email, profile_url)
    now = utc_now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            """
            INSERT INTO contacts (company_id, name, title, profile_url, email, source, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'manual', ?, ?)
            """,
            (company_id, name, clean_title, clean_url, clean_email, now, now),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ContactValidationError("This person is already saved for this company") from exc
    finally:
        conn.close()
    assert cursor.lastrowid is not None
    return get_contact(cursor.lastrowid)


def update_contact(contact_id: int, *, name: str, title: str, email: str, profile_url: str) -> Contact:
    get_contact(contact_id)
    name, clean_title, clean_email, clean_url = _clean(name, title, email, profile_url)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE contacts SET name=?, title=?, email=?, profile_url=?, updated_at=? WHERE id=?",
            (name, clean_title, clean_email, clean_url, utc_now(), contact_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ContactValidationError("Another saved person already has this profile link") from exc
    finally:
        conn.close()
    return get_contact(contact_id)


def delete_contact(contact_id: int) -> None:
    """Removes the person and every draft addressed to them."""
    get_contact(contact_id)
    conn = get_connection()
    try:
        conn.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        conn.commit()
    finally:
        conn.close()


# ---------- finding ----------


def find_people(company_id: str, company_name: str, city: str | None, roles: list[str]) -> FindResult:
    """One Exa people search for the profile's target roles at the company;
    stores the best few current employees. Costs one Exa search (~$0.007).
    """
    words = role_words(roles)
    query = f"{', '.join(roles) or 'Head of Sales, Founder'} at {company_name}"
    if city:
        query += f", {city}"
    try:
        results = exa_search(query, num_results=PEOPLE_RESULTS_PER_SEARCH, category="people")
    except requests.exceptions.RequestException as exc:
        raise RuntimeError(f"People search failed: {exc}") from exc

    candidates = [c for r in results if (c := candidate_from_result(r, company_name, words))]
    candidates.sort(key=lambda c: c.rank)  # stable: keeps Exa's relevance order within a rank

    added: list[Contact] = []
    already_saved = 0
    now = utc_now()
    conn = get_connection()
    try:
        for c in candidates:
            if len(added) >= MAX_CONTACTS_PER_SEARCH:
                break
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO contacts
                    (company_id, name, title, location, profile_url, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'exa_people', ?, ?)
                """,
                (company_id, c.name, c.title, c.location, c.profile_url, now, now),
            )
            if cursor.rowcount:
                assert cursor.lastrowid is not None
                row = conn.execute("SELECT * FROM contacts WHERE id=?", (cursor.lastrowid,)).fetchone()
                added.append(_from_row(row))
            else:
                already_saved += 1
        conn.commit()
    finally:
        conn.close()
    return FindResult(added=added, already_saved=already_saved, results_checked=len(results))
