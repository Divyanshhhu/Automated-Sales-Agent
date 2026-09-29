import pytest

from src import contacts
from src.contacts import (
    ContactValidationError,
    add_manual_contact,
    candidate_from_result,
    delete_contact,
    find_people,
    get_contact,
    list_contacts,
    role_words,
    same_company,
    title_rank,
    update_contact,
)
from src.profiles import Profile

ROLES = ["Head of Sales", "VP Sales", "Head of CRM", "Founder", "Director of Sales"]
WORDS = role_words(ROLES)


def test_role_words_keep_the_function_not_the_seniority() -> None:
    assert WORDS == {"sales", "crm", "founder"}
    assert role_words([]) == contacts.DEFAULT_ROLE_WORDS


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("Piramal Realty", "Piramal Realty", True),
        ("Tata Housing Development Company Limited", "Tata Housing Development", True),
        ("Tata Housing Development Co. Ltd.", "Tata Housing", True),
        ("Kumar World", "KUMAR PROPERTIES AND PROMOTERS PRIVATE LIMITED", False),  # live example
        ("Piramal Realty", "Kohinoor Group Pune", False),
        ("Adani", "Adanirealty", False),  # prefix must end on a word boundary
    ],
)
def test_same_company(a: str, b: str, same: bool) -> None:
    assert same_company(a, b) is same


@pytest.mark.parametrize(
    ("title", "rank"),
    [  # titles from the live Piramal / Kumar World searches
        ("Chief Sales & Service Officer", 0),
        ("Assistant Vice President - CRM", 0),
        ("Sales Head - Kumar Prime View Project", 0),
        ("Co-Founder", 0),
        ("Managing Director", 0),
        ("Senior Sales Manager", 1),
        ("Channel Sales", 1),
        ("Associate Vice President", None),  # senior, but no target function
        ("Marketing & Commercial P&L Head", None),
    ],
)
def test_title_rank(title: str, rank: int | None) -> None:
    assert title_rank(title, WORDS) == rank


def _person(
    name: str, jobs: list[tuple[str, str, str | None]], url: str = "https://www.linkedin.com/in/x"
) -> dict:
    return {
        "url": url,
        "entity": {
            "name": name,
            "location": "Mumbai, Maharashtra, India",
            "workHistory": [
                {"title": t, "company": {"name": c}, "dates": {"from": "2020-01-01", "to": to}}
                for t, c, to in jobs
            ],
        },
    }


def test_candidate_uses_current_job_at_this_company() -> None:
    result = _person(
        "Pankaj Mundada",
        [
            ("Chief Sales & Service Officer", "Piramal Realty", None),
            ("SEVP Head CRM", "Piramal Realty", "2024-06-01"),
        ],
    )
    candidate = candidate_from_result(result, "Piramal Realty", WORDS)
    assert candidate is not None
    assert (candidate.name, candidate.title, candidate.rank) == (
        "Pankaj Mundada",
        "Chief Sales & Service Officer",
        0,
    )


@pytest.mark.parametrize(
    "jobs",
    [
        [("Head of Sales", "Piramal Realty", "2022-01-01")],  # left the company
        [("Chief Revenue Officer", "Kohinoor Group Pune", None)],  # works elsewhere
        [("Associate Vice President", "Piramal Realty", None)],  # no target function
        [],
    ],
)
def test_non_candidates(jobs: list[tuple[str, str, str | None]]) -> None:
    assert candidate_from_result(_person("Someone", jobs), "Piramal Realty", WORDS) is None


def test_unsafe_profile_url_is_dropped() -> None:
    result = _person("A", [("Head of Sales", "Acme Realty", None)], url="javascript:alert(1)")
    candidate = candidate_from_result(result, "Acme Realty", WORDS)
    assert candidate is not None and candidate.profile_url is None


def test_find_people_stores_best_current_employees_once(
    profile: Profile, company_row: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[tuple[str, object]] = []

    def fake_exa(query: str, **kwargs: object) -> list[dict]:
        queries.append((query, kwargs.get("category")))
        people = [
            _person(f"Junior {i}", [("Sales Executive", "Acme Realty", None)], f"https://li/j{i}")
            for i in range(4)
        ]
        people += [
            _person("Boss", [("Head of Sales", "Acme Realty", None)], "https://li/boss"),
            _person("Gone", [("Head of Sales", "Acme Realty", "2021-01-01")], "https://li/gone"),
            _person("Founder", [("Founder & CEO", "Acme Realty Pvt Ltd", None)], "https://li/founder"),
        ]
        return people

    monkeypatch.setattr(contacts, "exa_search", fake_exa)
    result = find_people("c1", "Acme Realty", "Mumbai", ROLES)

    assert queries == [
        ("Head of Sales, VP Sales, Head of CRM, Founder, Director of Sales at Acme Realty, Mumbai", "people")
    ]
    names = [c.name for c in result.added]
    assert len(names) == contacts.MAX_CONTACTS_PER_SEARCH
    assert names[:2] == ["Boss", "Founder"]  # senior people first
    assert "Gone" not in names
    assert all(c.source == "exa_people" for c in result.added)

    again = find_people("c1", "Acme Realty", "Mumbai", ROLES)
    assert again.added[0].name == "Junior 3"  # the one that didn't fit last time
    assert again.already_saved == 5
    assert len(list_contacts("c1")) == 6


def test_manual_contact_validation_update_and_delete(profile: Profile, company_row: dict) -> None:
    contact = add_manual_contact("c1", name="  Asha Rao ", title="VP Sales", email="asha@acme.in")
    assert (contact.name, contact.first_name, contact.source) == ("Asha Rao", "Asha", "manual")

    with pytest.raises(ContactValidationError, match="email"):
        add_manual_contact("c1", name="X", email="not-an-email")
    with pytest.raises(ContactValidationError, match="https"):
        add_manual_contact("c1", name="X", profile_url="javascript:alert(1)")
    with pytest.raises(ContactValidationError, match="Name"):
        add_manual_contact("c1", name="   ")

    updated = update_contact(contact.id, name="Asha Rao", title="SVP Sales", email="", profile_url="")
    assert (updated.title, updated.email) == ("SVP Sales", None)

    delete_contact(contact.id)
    with pytest.raises(contacts.ContactNotFoundError):
        get_contact(contact.id)
