import pytest

from src.web.rendering import render_cited_text, render_research, safe_http_url


def test_research_uses_footnotes_marks_reading_and_pulls_out_gaps() -> None:
    text = (
        "They launched <3> towers [E7]. That means more enquiries [INFERENCE]. "
        "The sources don't show response times [NO_EVIDENCE]. Unknown source [E99]."
    )
    research = render_research(text, {7: 1})
    html = str(research.html)
    assert 'They launched &lt;3&gt; towers.<sup class="fn"><a href="#source-1">1</a></sup>' in html
    reading = (
        '<span class="reading"><span class="reading-tag">our reading</span> '
        "That means more enquiries.</span>"
    )
    assert reading in html
    assert "response times" not in html
    assert research.gaps == ["The sources don't show response times."]
    assert 'fn-bad' in html  # a citation to a source that doesn't exist is flagged


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
