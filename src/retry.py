"""Shared retry-with-backoff helper for external API calls.

One utility reused by discovery.py, evidence.py, and memo_generator.py
instead of duplicating retry logic per call site.
"""
import logging
import random
import time
from collections.abc import Callable
from typing import TypeVar

import requests

logger = logging.getLogger("sales_agent")

T = TypeVar("T")


def call_with_retry(
    fn: Callable[[], T],
    *,
    is_retriable: Callable[[Exception], bool],
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
    context: str = "",
) -> T:
    """Calls fn() with exponential backoff + jitter on retriable failures.

    Re-raises the final exception once attempts are exhausted, or immediately
    for a non-retriable exception (bad request, auth failure, etc.) -- those
    indicate a real bug or config problem, and retrying them just wastes
    time and money while hiding the actual issue.
    """
    attempt = 1
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= max_attempts or not is_retriable(exc):
                logger.error("Giving up after %d attempt(s) for %s: %s", attempt, context, exc)
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.5)
            logger.warning(
                "Attempt %d/%d failed for %s (%s) -- retrying in %.1fs",
                attempt, max_attempts, context, exc, delay,
            )
            time.sleep(delay)
            attempt += 1


def is_retriable_requests_error(exc: Exception) -> bool:
    """Retry on timeouts, connection errors, rate limits (429), and 5xx.

    Does NOT retry other 4xx errors (bad request, auth failure, not found,
    plan-access-denied) -- those need a real fix, not a retry.
    """
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        status = exc.response.status_code if exc.response is not None else None
        return status == 429 or (status is not None and 500 <= status < 600)
    return False
