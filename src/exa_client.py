"""Shared Exa search client, used for discovery (finding candidate company
domains). Exa's neural/entity search benchmarks meaningfully better than
Tavily specifically on company-discovery-style queries -- which is exactly
where our own testing hit the most noise (off-topic domains, dictionary and
entertainment sites turning up for a real-estate query). Evidence retrieval
(evidence.py) stays on Tavily, which is a better fit for its research-style
per-signal queries.
"""
import os

import requests

from .retry import call_with_retry, is_retriable_requests_error

EXA_URL = "https://api.exa.ai/search"


def exa_search(
    query: str,
    num_results: int = 8,
    category: str | None = None,
    exclude_domains: list[str] | None = None,
) -> list[dict]:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        raise RuntimeError("EXA_API_KEY is not set in the environment (.env)")

    payload = {
        "query": query,
        "numResults": num_results,
        "contents": {"text": {"maxCharacters": 500}},
    }
    if category:
        payload["category"] = category
    if exclude_domains:
        payload["excludeDomains"] = exclude_domains

    def _do_request():
        resp = requests.post(
            EXA_URL,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp

    resp = call_with_retry(
        _do_request, is_retriable=is_retriable_requests_error, context=f"Exa search({query!r})"
    )
    results = resp.json().get("results", [])
    # Normalize to the same shape Tavily results use (title/url/content), so
    # the filtering logic in discovery.py doesn't need to know which
    # provider produced a given result.
    return [
        {"title": r.get("title", "") or "", "url": r.get("url", ""), "content": r.get("text", "") or ""}
        for r in results
    ]
