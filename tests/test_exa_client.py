import pytest
import requests

from src import exa_client
from src.exa_client import exa_search


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.status_code = 200

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict:
        return self._payload


def test_normalizes_results_and_extracts_company_entity(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: dict = {}

    def fake_post(url: str, **kwargs: object) -> FakeResponse:
        sent.update(kwargs)
        return FakeResponse(
            {
                "results": [
                    {
                        "title": "Kolte-Patil",
                        "url": "https://koltepatil.com/",
                        "text": "Pune developer",
                        "entities": [{"type": "company", "properties": {"name": "Kolte-Patil"}}],
                    },
                    {"title": None, "url": "https://plain.in", "text": None},
                ]
            }
        )

    monkeypatch.setenv("EXA_API_KEY", "test-key")
    monkeypatch.setattr(requests, "post", fake_post)
    results = exa_search("q", category="company")

    assert results == [
        {
            "title": "Kolte-Patil",
            "url": "https://koltepatil.com/",
            "content": "Pune developer",
            "entity": {"name": "Kolte-Patil"},
        },
        {"title": "", "url": "https://plain.in", "content": "", "entity": None},
    ]
    payload = sent["json"]
    assert isinstance(payload, dict)
    assert payload["category"] == "company"
    # domain filters make Exa drop company entities -- they must never be sent
    assert "excludeDomains" not in payload
    assert "includeDomains" not in payload


def test_missing_api_key_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="EXA_API_KEY"):
        exa_client.exa_search("q")
