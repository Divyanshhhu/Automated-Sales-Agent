"""Export one profile's memos to CSV -- written to output/ after each run,
and downloadable from the review UI.
"""
import csv
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

from .db import get_connection

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"

HEADER = [
    "company_name", "domain", "location", "icp_fit_label", "icp_fit_score",
    "signal_confidence", "why_relevant", "potential_use_case",
    "citation_issues", "review_status", "review_notes",
]
# Cells starting with these are run as formulas by Excel/Sheets. Company
# names and memo text come from the open web, so neutralize them.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def filename_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "profile"


def _cell(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def write_memos_csv(profile_id: int, stream: TextIO) -> int:
    """Writes the profile's memos as CSV to `stream`. Returns the row count."""
    conn = get_connection()
    try:
        rows = conn.execute(
            """
            SELECT m.*, c.name AS company_name, c.domain, c.city, c.state, c.country
            FROM memos m JOIN companies c ON m.company_id = c.id
            WHERE m.profile_id = ?
            ORDER BY
                CASE m.icp_fit_label WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
                CASE m.signal_confidence
                    WHEN '\U0001F7E2 Strong signal' THEN 0
                    WHEN '\U0001F7E1 Plausible fit' THEN 1
                    ELSE 2
                END
            """,
            (profile_id,),
        ).fetchall()
    finally:
        conn.close()

    writer = csv.writer(stream)
    writer.writerow(HEADER)
    for r in rows:
        location = ", ".join([v for v in [r["city"], r["state"], r["country"]] if v])
        writer.writerow(
            [
                _cell(v)
                for v in (
                    r["company_name"], r["domain"], location, r["icp_fit_label"], r["icp_fit_score"],
                    r["signal_confidence"], r["why_relevant_text"], r["potential_use_case_text"],
                    r["citation_issues"] or "", r["review_status"], r["review_notes"] or "",
                )
            ]
        )
    return len(rows)


def export_memos_to_csv(profile_id: int, profile_name: str) -> str:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"memos_{filename_slug(profile_name)}_{ts}.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        write_memos_csv(profile_id, f)
    return str(out_path)
