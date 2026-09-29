"""Orchestrates Phase 1 end to end. A plain sequential script, not an agent --
per the architecture discussion, no stage here requires an autonomous
decision-maker, so a deterministic pipeline is the correct amount of
complexity.
"""
import logging
import sys
from pathlib import Path

from .config import load_config
from .db import get_connection
from .discovery import discover_companies, store_companies
from .evidence import get_evidence_for_company, retrieve_evidence_for_company
from .export import export_memos_to_csv
from .icp_scorer import score_all_pending
from .memo_generator import build_memo

logger = logging.getLogger("sales_agent")


def run_pipeline(config_path: str, discover_limit: int = 25) -> None:
    config = load_config(config_path)
    icp = config["icp"]
    product = config["product"]

    print(f"[1/5] Discovering companies matching ICP (up to {discover_limit})...")
    companies = discover_companies(icp, limit=discover_limit)
    company_ids = store_companies(companies)
    print(f"      Stored {len(company_ids)} companies.")

    print("[2/5] Scoring ICP fit (deterministic)...")
    scored_ids = score_all_pending(icp)
    print(f"      Scored {len(scored_ids)} companies.")

    conn = get_connection()
    threshold = icp.get("min_icp_fit_score", 50)
    qualifying = conn.execute(
        "SELECT * FROM companies WHERE icp_fit_score >= ? AND id NOT IN (SELECT company_id FROM memos)",
        (threshold,),
    ).fetchall()
    conn.close()
    print(f"      {len(qualifying)} companies clear the ICP fit threshold ({threshold}).")

    print("[3/5] Retrieving bounded evidence per qualifying company...")
    for row in qualifying:
        n = retrieve_evidence_for_company(row["id"], row["name"], config["signal_taxonomy"])
        print(f"      {row['name']}: {n} evidence items")

    print("[4/5] Generating grounded memos + validating citations...")
    for row in qualifying:
        evidence_items = get_evidence_for_company(row["id"])
        try:
            memo = build_memo(product, row, evidence_items)
        except Exception:
            logger.exception("Memo generation failed for %s -- skipping, batch continues", row["name"])
            print(f"      {row['name']}: FAILED (see log) -- skipped")
            continue
        flag = f" -- {len(memo['issues'])} citation issue(s), flagged for review" if memo["issues"] else ""
        print(f"      {row['name']}: {memo['signal_confidence']}{flag}")

    print("[5/5] Exporting to CSV...")
    out_path = export_memos_to_csv()
    print(f"      Done: {out_path}")


if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else str(
        Path(__file__).resolve().parent.parent / "config" / "product_profile.json"
    )
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    run_pipeline(cfg, limit)
