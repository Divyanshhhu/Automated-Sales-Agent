"""Runs the pipeline in a background thread so the UI can return
immediately and show progress.

One run at a time: runs spend real API credits and are bounded anyway, so a
second concurrent run is far more likely to be an accidental double-click
than a need. A plain in-process thread is enough for one local operator; a
job queue only earns its keep once runs must survive restarts or execute on
another machine.
"""
import logging
import threading

from ..pipeline import run_pipeline
from ..profiles import Profile
from ..runs import create_run

logger = logging.getLogger("sales_agent")

_lock = threading.Lock()
_thread: threading.Thread | None = None


class RunInProgressError(RuntimeError):
    pass


def start_background_run(profile: Profile, discover_limit: int) -> int:
    """Creates a queued run and starts it in the background. Returns the run id."""
    global _thread
    if not _lock.acquire(blocking=False):
        raise RunInProgressError("A run is already in progress -- wait for it to finish first")
    try:
        run_id = create_run(profile, discover_limit, status="queued")
    except BaseException:
        _lock.release()
        raise

    def _target() -> None:
        try:
            run_pipeline(profile, discover_limit, run_id=run_id)
        except BaseException:
            # run_pipeline has already marked the run failed; this is just the log
            logger.exception("Background run %d failed", run_id)
        finally:
            _lock.release()

    # daemon: stopping the server doesn't wait on a run; the next startup
    # marks the interrupted run failed (runs.fail_orphaned_runs).
    _thread = threading.Thread(target=_target, name=f"pipeline-run-{run_id}", daemon=True)
    _thread.start()
    return run_id


def wait_for_active_run(timeout: float | None = None) -> None:
    """Blocks until the current background run (if any) finishes."""
    if _thread is not None:
        _thread.join(timeout)
