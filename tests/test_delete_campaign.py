import copy

import pytest
from fastapi.testclient import TestClient

import run as cli
from src.contacts import add_manual_contact, list_contacts
from src.db import get_connection
from src.profiles import (
    Profile,
    ProfileInUseError,
    ProfileNotFoundError,
    create_profile,
    delete_profile,
    deletion_summary,
    get_profile,
    list_profiles,
)
from src.runs import complete_run, create_run
from src.usage import record_tavily, usage_context
from tests.conftest import VALID_CONFIG, insert_company, insert_evidence, insert_memo


def _count(table: str) -> int:
    conn = get_connection()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        conn.close()


def _fill(profile: Profile, company_id: str) -> None:
    """A campaign with a score, evidence, research, a contact, an email and a search."""
    conn = get_connection()
    conn.execute(
        "INSERT INTO company_scores (profile_id, company_id, score, label, breakdown_json, scored_at)"
        " VALUES (?, ?, 90, 'High', '{}', 't')",
        (profile.id, company_id),
    )
    conn.execute(
        "INSERT INTO evidence_searches (profile_id, company_id, category, query, results_kept, searched_at)"
        " VALUES (?, ?, 'expansion_launch', 'q', 1, 't')",
        (profile.id, company_id),
    )
    conn.commit()
    conn.close()
    insert_evidence(profile.id, company_id)
    memo_id = insert_memo(profile.id, company_id, status="approved")
    contact = add_manual_contact(company_id, name=f"Person at {company_id}")
    conn = get_connection()
    conn.execute(
        "INSERT INTO outreach_drafts (memo_id, contact_id, ref_code, generated_body, subject, body, status,"
        " created_at, updated_at) VALUES (?, ?, ?, 'x', 's', 'b', 'draft', 't', 't')",
        (memo_id, contact.id, f"SA-{profile.id}"),
    )
    conn.commit()
    conn.close()
    run_id = create_run(profile, 5)
    with usage_context("find_leads", profile_id=profile.id, run_id=run_id):
        record_tavily(1)
    complete_run(run_id, {})


@pytest.fixture
def two_campaigns(temp_db: object) -> tuple[Profile, Profile]:
    insert_company("shared.in", "Shared Realty")
    keep = create_profile("Keep", copy.deepcopy(VALID_CONFIG))
    doomed = create_profile("Doomed", copy.deepcopy(VALID_CONFIG))
    _fill(keep, "shared.in")
    _fill(doomed, "shared.in")
    return keep, doomed


def test_summary_counts_what_will_go(two_campaigns: tuple[Profile, Profile]) -> None:
    _, doomed = two_campaigns
    s = deletion_summary(doomed.id)
    assert (s.researched_companies, s.scored_companies, s.evidence_items, s.email_drafts, s.searches) == (
        1,
        1,
        1,
        1,
        1,
    )


def test_delete_removes_only_that_campaign(two_campaigns: tuple[Profile, Profile]) -> None:
    keep, doomed = two_campaigns
    delete_profile(doomed.id)

    with pytest.raises(ProfileNotFoundError):
        get_profile(doomed.id)
    assert [p.name for p in list_profiles()] == ["Keep"]
    # the other campaign's data is untouched
    s = deletion_summary(keep.id)
    assert (s.researched_companies, s.scored_companies, s.evidence_items, s.email_drafts, s.searches) == (
        1,
        1,
        1,
        1,
        1,
    )
    assert _count("evidence_searches") == 1
    # shared things stay: the company, both people found there, and all spending history
    assert _count("companies") == 1
    assert len(list_contacts("shared.in")) == 2
    conn = get_connection()
    usage = conn.execute("SELECT profile_id, run_id FROM api_usage ORDER BY id").fetchall()
    conn.close()
    assert [(r["profile_id"] is None, r["run_id"] is None) for r in usage] == [(False, False), (True, True)]


def test_cannot_delete_while_a_search_runs(two_campaigns: tuple[Profile, Profile]) -> None:
    _, doomed = two_campaigns
    create_run(doomed, 5)  # status "running"
    with pytest.raises(ProfileInUseError):
        delete_profile(doomed.id)
    assert deletion_summary(doomed.id).researched_companies == 1  # nothing was removed


# ---------- web ----------


def test_web_delete_needs_the_exact_name(client: TestClient, two_campaigns: tuple[Profile, Profile]) -> None:
    _, doomed = two_campaigns
    page = client.get(f"/profiles/{doomed.id}/delete").text
    assert "Delete “Doomed”?" in page and "1 email draft" in page

    wrong = client.post(f"/profiles/{doomed.id}/delete", data={"confirm_name": "doomed"})
    assert wrong.status_code == 422
    assert "doesn&#39;t match" in wrong.text
    assert get_profile(doomed.id)

    right = client.post(
        f"/profiles/{doomed.id}/delete", data={"confirm_name": " Doomed "}, follow_redirects=False
    )
    assert right.status_code == 303
    assert "Campaign “Doomed” deleted." in client.get(right.headers["location"]).text
    assert [p.name for p in list_profiles()] == ["Keep"]


def test_web_delete_refused_while_searching(
    client: TestClient, two_campaigns: tuple[Profile, Profile]
) -> None:
    _, doomed = two_campaigns
    create_run(doomed, 5)
    response = client.post(f"/profiles/{doomed.id}/delete", data={"confirm_name": "Doomed"})
    assert response.status_code == 409
    assert "A search is running" in response.text


def test_cross_site_delete_is_blocked(client: TestClient, two_campaigns: tuple[Profile, Profile]) -> None:
    _, doomed = two_campaigns
    response = client.post(
        f"/profiles/{doomed.id}/delete",
        data={"confirm_name": "Doomed"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    assert get_profile(doomed.id)


# ---------- command line ----------


def test_cli_delete_asks_for_the_name(
    two_campaigns: tuple[Profile, Profile],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("builtins.input", lambda _: "nope")
    with pytest.raises(SystemExit, match="didn't match"):
        cli.main(["delete-profile", "Doomed"])
    assert len(list_profiles()) == 2

    monkeypatch.setattr("builtins.input", lambda _: "Doomed")
    cli.main(["delete-profile", "Doomed"])
    assert "Deleted campaign 'Doomed'" in capsys.readouterr().out
    assert [p.name for p in list_profiles()] == ["Keep"]
