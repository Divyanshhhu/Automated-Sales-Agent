"""Orchestrates one pipeline run for one profile. A plain sequential script,
not an agent -- no stage here requires an autonomous decision-maker, so a
deterministic pipeline is the correct amount of complexity.

Every run is recorded in the runs table (see runs.py), including failures,
so a crashed or interrupted run is visible rather than silently missing.
"""
import logging

from .db import get_connection
from .discovery import discover_companies, store_companies
from .evidence import get_evidence_for_company, retrieve_evidence_for_company
from .export import export_memos_to_csv
from .icp_scorer import score_companies_for_profile
from .memo_generator import build_memo
from .profiles import Profile
from .runs import complete_run, create_run, fail_run, mark_running, update_stats

logger = logging.getLogger("sales_agent")


def _qualifying_companies(profile_id: int, threshold: float) -> list:
    """Companies that clear this profile's threshold and don't have a memo
    for this profile yet -- so a rerun picks up where a failed one stopped.
    """
    conn = get_connection()
    try:
        return conn.execute(
            """
            SELECT c.*, s.score AS icp_fit_score, s.label AS icp_fit_label
            FROM company_scores s JOIN companies c ON c.id = s.company_id
            WHERE s.profile_id = ? AND s.score >= ?
              AND NOT EXISTS (
                  SELECT 1 FROM memos m WHERE m.profile_id = s.profile_id AND m.company_id = s.company_id
              )
            ORDER BY s.score DESC, c.name
            """,
            (profile_id, threshold),
        ).fetchall()
    finally:
        conn.close()


def run_pipeline(profile: Profile, discover_limit: int = 25, *, run_id: int | None = None) -> int:
    """Runs the pipeline for one profile. Returns the run id.

    Pass run_id to execute a run the caller already created (the web UI
    creates it as "queued" so it can redirect to the progress page first);
    otherwise a new run record is created here.
    """
    config = profile.config
    icp = config["icp"]
    product = config["product"]
    if run_id is None:
        run_id = create_run(profile, discover_limit)
    else:
        mark_running(run_id)
    stats: dict = {}
    logger.info("Run %d started for profile %r (limit %d)", run_id, profile.name, discover_limit)

    def checkpoint(stage: str) -> None:
        stats["stage"] = stage
        update_stats(run_id, stats)

    try:
        checkpoint("discovering")
        print(f"[1/5] Discovering companies matching ICP (up to {discover_limit})...")
        companies = discover_companies(icp, limit=discover_limit)
        company_ids = store_companies(companies)
        stats["discovered"] = len(company_ids)
        print(f"      Stored {len(company_ids)} companies.")

        checkpoint("scoring")
        print("[2/5] Scoring ICP fit (deterministic)...")
        stats["scored"] = score_companies_for_profile(profile.id, icp, company_ids)
        threshold = icp.get("min_icp_fit_score", 50)
        qualifying = _qualifying_companies(profile.id, threshold)
        stats["qualifying"] = len(qualifying)
        print(f"      Scored {stats['scored']} companies.")
        print(
            f"      {len(qualifying)} companies clear the ICP fit threshold ({threshold}) "
            "and still need a memo."
        )

        stats["evidence_added"] = 0
        stats["evidence_done"] = 0
        checkpoint("evidence")
        print("[3/5] Retrieving bounded evidence per qualifying company...")
        for row in qualifying:
            n = retrieve_evidence_for_company(profile.id, row["id"], row["name"], config["signal_taxonomy"])
            stats["evidence_added"] += n
            stats["evidence_done"] += 1
            checkpoint("evidence")
            print(f"      {row['name']}: {n} new evidence items")

        stats["memos_generated"] = 0
        stats["memos_failed"] = 0
        checkpoint("memos")
        print("[4/5] Generating grounded memos + validating citations...")
        for row in qualifying:
            evidence_items = get_evidence_for_company(profile.id, row["id"])
            try:
                memo = build_memo(product, row, evidence_items, profile_id=profile.id, run_id=run_id)
            except Exception:
                stats["memos_failed"] += 1
                checkpoint("memos")
                logger.exception("Memo generation failed for %s -- skipping, batch continues", row["name"])
                print(f"      {row['name']}: FAILED (see log) -- skipped")
                continue
            stats["memos_generated"] += 1
            checkpoint("memos")
            issues = memo["issues"]
            flag = f" -- {len(issues)} citation issue(s), flagged for review" if issues else ""
            print(f"      {row['name']}: {memo['signal_confidence']}{flag}")

        checkpoint("exporting")
        print("[5/5] Exporting to CSV...")
        stats["csv_path"] = export_memos_to_csv(profile.id, profile.name)
        print(f"      Done: {stats['csv_path']}")
    except BaseException as exc:  # includes Ctrl+C: the run must not stay "running" forever
        fail_run(run_id, stats, f"{type(exc).__name__}: {exc}")
        logger.error("Run %d failed: %s", run_id, exc)
        raise

    stats["stage"] = "done"
    complete_run(run_id, stats)
    logger.info("Run %d succeeded: %s", run_id, stats)
    return run_id
