from pathlib import Path

import pytest

from src.config import ConfigError
from src.profiles import (
    InvalidProfileNameError,
    ProfileExistsError,
    ProfileNotFoundError,
    create_profile,
    get_profile,
    get_profile_by_name,
    list_profiles,
    update_profile,
)


def test_create_and_fetch(temp_db: Path, valid_config: dict) -> None:
    created = create_profile("  Mumbai mid-size  ", valid_config)
    assert created.name == "Mumbai mid-size"
    assert created.config == valid_config
    assert get_profile(created.id) == created
    assert get_profile_by_name("Mumbai mid-size") == created


def test_list_is_sorted_by_name(temp_db: Path, valid_config: dict) -> None:
    create_profile("b", valid_config)
    create_profile("a", valid_config)
    assert [p.name for p in list_profiles()] == ["a", "b"]


def test_duplicate_name_rejected(temp_db: Path, valid_config: dict) -> None:
    create_profile("Default", valid_config)
    with pytest.raises(ProfileExistsError):
        create_profile("Default", valid_config)


@pytest.mark.parametrize("name", ["", "   ", "x" * 101])
def test_invalid_name_rejected(temp_db: Path, valid_config: dict, name: str) -> None:
    with pytest.raises(InvalidProfileNameError):
        create_profile(name, valid_config)


def test_invalid_config_rejected_and_not_stored(temp_db: Path, valid_config: dict) -> None:
    valid_config["icp"]["employee_range"] = [2000, 51]
    with pytest.raises(ConfigError):
        create_profile("Bad", valid_config)
    assert list_profiles() == []


def test_update_config_and_name(temp_db: Path, valid_config: dict) -> None:
    profile = create_profile("Old", valid_config)
    new_config = {**valid_config, "icp": {**valid_config["icp"], "geographies": ["Delhi"]}}
    updated = update_profile(profile.id, name="New", config=new_config)
    assert updated.name == "New"
    assert updated.config["icp"]["geographies"] == ["Delhi"]
    assert updated.created_at == profile.created_at


def test_update_rejects_invalid_config_and_keeps_old(temp_db: Path, valid_config: dict) -> None:
    profile = create_profile("P", valid_config)
    with pytest.raises(ConfigError):
        update_profile(profile.id, config={"product": {}})
    assert get_profile(profile.id).config == valid_config


def test_rename_to_existing_name_rejected(temp_db: Path, valid_config: dict) -> None:
    create_profile("A", valid_config)
    b = create_profile("B", valid_config)
    with pytest.raises(ProfileExistsError):
        update_profile(b.id, name="A")


def test_missing_profile(temp_db: Path) -> None:
    with pytest.raises(ProfileNotFoundError):
        get_profile(999)
    with pytest.raises(ProfileNotFoundError):
        get_profile_by_name("nope")
