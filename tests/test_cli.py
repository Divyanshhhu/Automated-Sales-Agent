import json
from pathlib import Path

import pytest

import run as cli
from src.profiles import get_profile_by_name
from tests.conftest import VALID_CONFIG


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(VALID_CONFIG), encoding="utf-8")
    return path


def test_import_list_and_export_roundtrip(
    temp_db: Path, config_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.main(["import-profile", str(config_file), "--name", "Default"])
    cli.main(["profiles"])
    assert "Default" in capsys.readouterr().out

    out = tmp_path / "exported.json"
    cli.main(["export-profile", "Default", str(out)])
    assert json.loads(out.read_text(encoding="utf-8")) == VALID_CONFIG


def test_import_existing_name_requires_replace(temp_db: Path, config_file: Path) -> None:
    cli.main(["import-profile", str(config_file), "--name", "Default"])
    with pytest.raises(SystemExit, match="--replace"):
        cli.main(["import-profile", str(config_file), "--name", "Default"])

    changed = {**VALID_CONFIG, "icp": {**VALID_CONFIG["icp"], "geographies": ["Delhi"]}}
    config_file.write_text(json.dumps(changed), encoding="utf-8")
    cli.main(["import-profile", str(config_file), "--name", "Default", "--replace"])
    assert get_profile_by_name("Default").config["icp"]["geographies"] == ["Delhi"]


def test_run_unknown_profile_exits_with_message(temp_db: Path) -> None:
    with pytest.raises(SystemExit, match="No profile named 'Nope'"):
        cli.main(["run", "--profile", "Nope"])


def test_invalid_config_file_exits_with_message(temp_db: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"product": {}}), encoding="utf-8")
    with pytest.raises(SystemExit, match="Invalid config"):
        cli.main(["import-profile", str(bad), "--name", "Bad"])


@pytest.mark.parametrize("limit", ["0", "-3", "abc"])
def test_limit_must_be_positive_integer(temp_db: Path, limit: str) -> None:
    with pytest.raises(SystemExit):
        cli.main(["run", "--profile", "Default", "--limit", limit])


def test_no_profiles_prints_import_hint(temp_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cli.main(["profiles"])
    assert "import-profile" in capsys.readouterr().out
