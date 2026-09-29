"""Export memos to CSV for human review. No dashboard in V1 -- building a
review UI before the memos are proven useful would be infrastructure ahead
of validated value.
"""
import csv
from datetime import datetime, timezone
from pathlib import Path

from .db import get_connection

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "output"


def export_memos_to_csv() -> str:
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT m.*, c.name AS company_name, c.domain, c.city, c.state, c.country
        FROM memos m JOIN companies c ON m.company_id = c.id
        ORDER BY
            CASE m.icp_fit_label WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
            CASE m.signal_confidence
                WHEN '\U0001F7E2 Strong signal' THEN 0
                WHEN '\U0001F7E1 Plausible fit' THEN 1
                ELSE 2
            END
        """
    ).fetchall()
    conn.close()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"memos_{ts}.csv"

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "company_name", "domain", "location", "icp_fit_label", "icp_fit_score",
                "signal_confidence", "why_relevant", "potential_use_case",
                "citation_issues", "review_status",
            ]
        )
        for r in rows:
            location = ", ".join([v for v in [r["city"], r["state"], r["country"]] if v])
            writer.writerow(
                [
                    r["company_name"], r["domain"], location, r["icp_fit_label"], r["icp_fit_score"],
                    r["signal_confidence"], r["why_relevant_text"], r["potential_use_case_text"],
                    r["citation_issues"] or "", r["review_status"],
                ]
            )
    return str(out_path)
