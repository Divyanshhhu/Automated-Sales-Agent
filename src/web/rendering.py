"""Safe HTML rendering of memo text and evidence links.

Memo text is LLM output built from open-web content, so it is escaped
first; only then are citation tags turned into markup this module writes
itself. Nothing from the memo is ever inserted unescaped.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote, urlparse

from markupsafe import Markup, escape

from ..citations import TAG_RE, sentences, strip_tags

_LABELS = {"INFERENCE": "inference", "NO_EVIDENCE": "no evidence", "PRODUCT": "about your product"}


def render_cited_text(text: str, evidence_ids: set[int]) -> Markup:
    """Escapes `text`, then turns [E12] into a link to that evidence item on
    the page and [INFERENCE] / [NO_EVIDENCE] / [PRODUCT] into labelled tags. A citation
    to evidence that doesn't exist is marked, not linked.
    """

    def replace(match: re.Match[str]) -> str:
        tag, number = match.group(1), match.group(2)
        if number:
            if int(number) in evidence_ids:
                return f'<a class="cite" href="#E{number}">[E{number}]</a>'
            return f'<span class="cite cite-bad" title="No such evidence item">[E{number}]</span>'
        return f'<span class="tag tag-{tag.lower()}">{_LABELS[tag]}</span>'

    return Markup(TAG_RE.sub(replace, str(escape(text))))


@dataclass(frozen=True)
class ResearchText:
    html: Markup  # the readable text, with footnotes
    gaps: list[str]  # what the sources don't show ([NO_EVIDENCE] sentences), untagged


def render_research(text: str, footnotes: dict[int, int]) -> ResearchText:
    """Research text for reading: each sentence citing evidence gets a small
    numbered footnote (`footnotes` maps evidence id -> number shown), the
    model's own reasoning ([INFERENCE]) is marked "our reading", and "the
    sources don't show..." sentences ([NO_EVIDENCE]) are pulled out into
    `gaps` for a separate note. Text is escaped before any markup is added.
    """
    parts: list[str] = []
    gaps: list[str] = []
    for sentence in sentences(text):
        tags = TAG_RE.findall(sentence)
        plain = strip_tags(sentence)
        if any(full == "NO_EVIDENCE" for full, _ in tags):
            gaps.append(plain)
            continue
        body = str(escape(plain))
        refs = []
        for _, number in tags:
            if not number:
                continue
            shown = footnotes.get(int(number))
            refs.append(
                f'<sup class="fn"><a href="#source-{shown}">{shown}</a></sup>'
                if shown
                else '<sup class="fn fn-bad" title="Cites a source that doesn\'t exist">?</sup>'
            )
        if refs:
            parts.append(body + "".join(refs))
        elif any(full == "INFERENCE" for full, _ in tags):
            parts.append(f'<span class="reading"><span class="reading-tag">our reading</span> {body}</span>')
        else:
            parts.append(body)
    return ResearchText(html=Markup(" ".join(parts)), gaps=gaps)


def mailto_link(email: str | None, subject: str, body: str) -> str | None:
    """Opens the user's own email app with the draft filled in; None without an address."""
    if not email:
        return None
    return f"mailto:{quote(email, safe='@')}?subject={quote(subject, safe='')}&body={quote(body, safe='')}"


def safe_http_url(url: str | None) -> str | None:
    """The URL if it's http(s), else None -- evidence URLs come from search
    results, and a javascript: URL must never become a clickable link.
    """
    if not url:
        return None
    try:
        scheme = urlparse(url).scheme.lower()
    except ValueError:
        return None
    return url if scheme in ("http", "https") else None
