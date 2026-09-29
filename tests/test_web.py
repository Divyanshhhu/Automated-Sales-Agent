import copy
import json
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src import runs
from src.profiles import Profile, create_profile, get_profile, get_profile_by_name, list_profiles
from src.web import app as app_module
from src.web import runner
from src.web.app import create_app
from src.web.forms import config_to_form
from tests.conftest import VALID_CONFIG

BASE_URL = "http://127.0.0.1"


@pytest.fixture
def starter_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "starter.json"
    path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    monkeypatch.setattr(app_module, "DEFAULT_PROFILE_PATH", path)
    return path


@pytest.fixture
def client(temp_db: Path, starter_path: Path) -> Iterator[TestClient]:
    with TestClient(create_app(), base_url=BASE_URL) as test_client:
        yield test_client
    runner.wait_for_active_run(timeout=5)


def _form(config: dict, name: str) -> dict[str, str]:
    return {**config_to_form(config), "name": name}


# ---------- pages ----------


def test_home_redirects_to_profiles(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/profiles"


def test_empty_profiles_page(client: TestClient) -> None:
    response = client.get("/profiles")
    assert response.status_code == 200
    assert "No profiles yet" in response.text


def test_new_profile_form_is_prefilled_from_starter(client: TestClient) -> None:
    response = client.get("/profiles/new")
    assert response.status_code == 200
    assert "Test Product" in response.text
    assert "expansion_launch: {company} new project launch" in response.text


# ---------- create / edit ----------


def test_create_profile(client: TestClient) -> None:
    response = client.post("/profiles", data=_form(VALID_CONFIG, "Mumbai mid-size"), follow_redirects=False)
    assert response.status_code == 303
    profile = get_profile_by_name("Mumbai mid-size")
    assert profile.config["icp"] == {**VALID_CONFIG["icp"], "industries": [], "roles": []}
    assert profile.config["signal_taxonomy"] == VALID_CONFIG["signal_taxonomy"]
    assert "Profile saved" in client.get(response.headers["location"]).text


def test_invalid_form_shows_errors_next_to_fields_and_saves_nothing(client: TestClient) -> None:
    form = _form(VALID_CONFIG, "Broken")
    form["icp.employee_min"] = "fifty"
    form["signal_taxonomy"] = "not a valid line"
    response = client.post("/profiles", data=form)
    assert response.status_code == 422
    assert "Employee range needs whole numbers" in response.text
    assert "must look like" in response.text
    assert 'value="fifty"' in response.text  # the user's input is kept, not wiped
    assert list_profiles() == []


def test_config_validation_errors_are_shown(client: TestClient) -> None:
    form = _form(VALID_CONFIG, "Backwards")
    form["icp.employee_min"], form["icp.employee_max"] = "2000", "51"
    form["icp.geographies"] = ""
    response = client.post("/profiles", data=form)
    assert response.status_code == 422
    assert "icp.employee_range must satisfy" in response.text
    assert "icp.geographies must be a non-empty list" in response.text


def test_duplicate_name_is_rejected(client: TestClient) -> None:
    create_profile("Taken", copy.deepcopy(VALID_CONFIG))
    response = client.post("/profiles", data=_form(VALID_CONFIG, "Taken"))
    assert response.status_code == 422
    assert "already exists" in response.text


def test_edit_profile_updates_and_keeps_unknown_keys(client: TestClient) -> None:
    config = copy.deepcopy(VALID_CONFIG)
    config["icp"]["future_field"] = "keep"
    profile = create_profile("Editable", config)

    page = client.get(f"/profiles/{profile.id}/edit")
    assert page.status_code == 200
    assert "Editable" in page.text

    form = _form(config, "Renamed")
    form["icp.geographies"] = "Delhi\nGurugram"
    response = client.post(f"/profiles/{profile.id}", data=form, follow_redirects=False)
    assert response.status_code == 303
    updated = get_profile(profile.id)
    assert updated.name == "Renamed"
    assert updated.config["icp"]["geographies"] == ["Delhi", "Gurugram"]
    assert updated.config["icp"]["future_field"] == "keep"


def test_duplicate_profile(client: TestClient) -> None:
    profile = create_profile("Base", copy.deepcopy(VALID_CONFIG))
    first = client.post(f"/profiles/{profile.id}/duplicate", follow_redirects=False)
    client.post(f"/profiles/{profile.id}/duplicate")
    assert first.status_code == 303
    assert {p.name for p in list_profiles()} == {"Base", "Base (copy)", "Base (copy 2)"}
    assert get_profile_by_name("Base (copy)").config == profile.config


# ---------- import / export ----------


def test_export_profile(client: TestClient) -> None:
    profile = create_profile("Mumbai Mid-size!", copy.deepcopy(VALID_CONFIG))
    response = client.get(f"/profiles/{profile.id}/export")
    assert response.status_code == 200
    assert response.json() == VALID_CONFIG
    assert 'filename="mumbai-mid-size.json"' in response.headers["content-disposition"]


def test_import_profile(client: TestClient) -> None:
    response = client.post(
        "/profiles/import",
        data={"name": "Imported"},
        files={"file": ("p.json", json.dumps(VALID_CONFIG), "application/json")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert get_profile_by_name("Imported").config == VALID_CONFIG


@pytest.mark.parametrize(
    ("content", "message"),
    [("{not json", "not valid JSON"), (json.dumps({"product": {}}), "Invalid config")],
)
def test_import_rejects_bad_files(client: TestClient, content: str, message: str) -> None:
    response = client.post(
        "/profiles/import", data={"name": "Bad"}, files={"file": ("p.json", content, "application/json")}
    )
    assert response.status_code == 422
    assert message in response.text
    assert list_profiles() == []


# ---------- runs ----------


def _fake_pipeline(profile: Profile, limit: int, *, run_id: int) -> int:
    runs.mark_running(run_id)
    runs.complete_run(run_id, {"stage": "done", "discovered": 3, "qualifying": 2, "memos_generated": 2})
    return run_id


def test_start_run_and_view_result(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "run_pipeline", _fake_pipeline)
    profile = create_profile("P", copy.deepcopy(VALID_CONFIG))

    data = {"profile_id": str(profile.id), "limit": "5"}
    response = client.post("/runs", data=data, follow_redirects=False)
    assert response.status_code == 303
    run_id = int(response.headers["location"].rsplit("/", 1)[1])
    runner.wait_for_active_run(timeout=5)

    run = runs.get_run(run_id)
    assert (run.status, run.discover_limit, run.profile_id) == ("succeeded", 5, profile.id)
    page = client.get(f"/runs/{run_id}")
    assert "succeeded" in page.text
    assert "hx-trigger" not in page.text  # finished runs stop polling
    assert "succeeded" in client.get("/runs").text


def test_active_run_status_keeps_polling(client: TestClient) -> None:
    profile = create_profile("P", copy.deepcopy(VALID_CONFIG))
    run_id = runs.create_run(profile, 5, status="running")
    runs.update_stats(run_id, {"stage": "evidence", "qualifying": 4, "evidence_done": 1})
    response = client.get(f"/runs/{run_id}/status")
    assert response.status_code == 200
    assert 'hx-trigger="every 2s"' in response.text
    assert "1 / 4" in response.text


def test_second_run_while_one_is_active_is_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()

    def slow_pipeline(profile: Profile, limit: int, *, run_id: int) -> int:
        release.wait(timeout=5)
        return _fake_pipeline(profile, limit, run_id=run_id)

    monkeypatch.setattr(runner, "run_pipeline", slow_pipeline)
    profile = create_profile("P", copy.deepcopy(VALID_CONFIG))
    data = {"profile_id": str(profile.id), "limit": "5"}
    try:
        assert client.post("/runs", data=data, follow_redirects=False).status_code == 303
        second = client.post("/runs", data=data)
        assert second.status_code == 409
        assert "already in progress" in second.text
    finally:
        release.set()
        runner.wait_for_active_run(timeout=5)
    assert client.post("/runs", data=data, follow_redirects=False).status_code == 303  # lock released


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
def test_run_limit_is_validated(client: TestClient, limit: str) -> None:
    profile = create_profile("P", copy.deepcopy(VALID_CONFIG))
    response = client.post("/runs", data={"profile_id": str(profile.id), "limit": limit})
    assert response.status_code == 422
    assert runs.list_runs() == []


def test_server_start_marks_interrupted_runs_failed(temp_db: Path, starter_path: Path) -> None:
    profile = create_profile("P", copy.deepcopy(VALID_CONFIG))
    run_id = runs.create_run(profile, 5, status="running")
    with TestClient(create_app(), base_url=BASE_URL):
        run = runs.get_run(run_id)
    assert run.status == "failed"
    assert run.error is not None and "Interrupted" in run.error


def test_missing_things_are_404(client: TestClient) -> None:
    assert client.get("/profiles/999/edit").status_code == 404
    assert client.get("/runs/999").status_code == 404
    assert client.post("/profiles/999/duplicate").status_code == 404


# ---------- local-only guards ----------


@pytest.mark.parametrize(
    "headers",
    [
        {"origin": "https://evil.example"},
        {"referer": "https://evil.example/page"},
        {"sec-fetch-site": "cross-site"},
    ],
)
def test_cross_site_writes_are_rejected(client: TestClient, headers: dict[str, str]) -> None:
    response = client.post("/profiles", data=_form(VALID_CONFIG, "Sneaky"), headers=headers)
    assert response.status_code == 403
    assert list_profiles() == []


def test_same_origin_writes_are_allowed(client: TestClient) -> None:
    response = client.post(
        "/profiles",
        data=_form(VALID_CONFIG, "Legit"),
        headers={"origin": BASE_URL, "sec-fetch-site": "same-origin"},
        follow_redirects=False,
    )
    assert response.status_code == 303


def test_unexpected_host_header_is_rejected(client: TestClient) -> None:
    assert client.get("/profiles", headers={"host": "attacker.example"}).status_code == 400
