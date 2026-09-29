import pytest

from src.web.rendering import render_cited_text, safe_http_url


def test_known_citation_becomes_link() -> None:
    html = str(render_cited_text("New tower [E12].", {12}))
    assert html == 'New tower <a class="cite" href="#E12">[E12]</a>.'


def test_unknown_citation_is_marked_not_linked() -> None:
    html = str(render_cited_text("Claim [E99].", {12}))
    assert "cite-bad" in html
    assert 'href="#E99"' not in html


def test_inference_and_no_evidence_tags() -> None:
    html = str(render_cited_text("Guess [INFERENCE]. Gap [NO_EVIDENCE].", set()))
    assert '<span class="tag tag-inference">inference</span>' in html
    assert '<span class="tag tag-no_evidence">no evidence</span>' in html


def test_memo_text_is_escaped_before_markup_is_added() -> None:
    html = str(render_cited_text('<script>alert(1)</script> "x" [E1]', {1}))
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert '<a class="cite" href="#E1">[E1]</a>' in html


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://news.example/a", "https://news.example/a"),
        ("http://news.example/a", "http://news.example/a"),
        ("javascript:alert(1)", None),
        ("JavaScript:alert(1)", None),
        ("data:text/html,<b>x</b>", None),
        ("", None),
        (None, None),
    ],
)
def test_safe_http_url(url: str | None, expected: str | None) -> None:
    assert safe_http_url(url) == expected
