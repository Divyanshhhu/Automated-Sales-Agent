"""Named product + ICP profiles, stored in the database.

A profile holds exactly what product_profile.json holds; that file format is
now the import/export format. Every write goes through validate_config, so
a profile in the database is always one the pipeline can run.
"""
import json
import sqlite3
from dataclasses import dataclass

from .config import validate_config
from .db import get_connection, utc_now

MAX_NAME_LENGTH = 100


class ProfileNotFoundError(LookupError):
    pass


class ProfileExistsError(ValueError):
    pass


class InvalidProfileNameError(ValueError):
    pass


@dataclass(frozen=True)
class Profile:
    id: int
    name: str
    config: dict
    created_at: str
    updated_at: str


def _clean_name(name: str) -> str:
    cleaned = name.strip() if isinstance(name, str) else ""
    if not cleaned:
        raise InvalidProfileNameError("Profile name must not be empty")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidProfileNameError(f"Profile name must be at most {MAX_NAME_LENGTH} characters")
    return cleaned


def _from_row(row: sqlite3.Row) -> Profile:
    return Profile(
        id=row["id"],
        name=row["name"],
        config=json.loads(row["config_json"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def create_profile(name: str, config: dict) -> Profile:
    name = _clean_name(name)
    validate_config(config)
    now = utc_now()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "INSERT INTO profiles (name, config_json, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, json.dumps(config), now, now),
        )
        conn.commit()
        profile_id = cursor.lastrowid
    except sqlite3.IntegrityError as exc:
        raise ProfileExistsError(f"A profile named {name!r} already exists") from exc
    finally:
        conn.close()
    assert profile_id is not None
    return get_profile(profile_id)


def update_profile(profile_id: int, *, name: str | None = None, config: dict | None = None) -> Profile:
    """Changes apply to future runs only: each run keeps a snapshot of the
    config it actually used (see runs.py).
    """
    current = get_profile(profile_id)
    new_name = _clean_name(name) if name is not None else current.name
    new_config = config if config is not None else current.config
    validate_config(new_config)
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE profiles SET name=?, config_json=?, updated_at=? WHERE id=?",
            (new_name, json.dumps(new_config), utc_now(), profile_id),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        raise ProfileExistsError(f"A profile named {new_name!r} already exists") from exc
    finally:
        conn.close()
    return get_profile(profile_id)


def get_profile(profile_id: int) -> Profile:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProfileNotFoundError(f"No profile with id {profile_id}")
    return _from_row(row)


def get_profile_by_name(name: str) -> Profile:
    conn = get_connection()
    try:
        row = conn.execute("SELECT * FROM profiles WHERE name=?", (name.strip(),)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProfileNotFoundError(f"No profile named {name!r}")
    return _from_row(row)


def list_profiles() -> list[Profile]:
    conn = get_connection()
    try:
        rows = conn.execute("SELECT * FROM profiles ORDER BY name").fetchall()
    finally:
        conn.close()
    return [_from_row(r) for r in rows]
