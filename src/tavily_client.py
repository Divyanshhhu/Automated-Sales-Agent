"""Shared Tavily search client, used by evidence.py (per-category signal
retrieval). Discovery uses Exa instead -- see exa_client.py.
"""
import os

import requests

from .retry import call_with_retry, is_retriable_requests_error
from .usage import record_tavily

TAVILY_URL = "https://api.tavily.com/search"


def tavily_search(query: str, max_results: int = 5, **extra_params) -> list[dict]:
    api_key = os.environ.get("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError("TAVILY_API_KEY is not set in the environment (.env)")

    payload = {"query": query, "search_depth": "basic", "max_results": max_results, "topic": "general"}
    payload.update(extra_params)

    def _do_request():
        resp = requests.post(
            TAVILY_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        return resp

    resp = call_with_retry(
        _do_request, is_retriable=is_retriable_requests_error, context=f"Tavily search({query!r})"
    )
    # Tavily bills per request: 1 credit for basic depth, 2 for advanced.
    record_tavily(2 if payload.get("search_depth") == "advanced" else 1, detail=query)
    return resp.json().get("results", [])
