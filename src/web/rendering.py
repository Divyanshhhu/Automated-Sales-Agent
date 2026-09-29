"""Safe HTML rendering of memo text and evidence links.

Memo text is LLM output built from open-web content, so it is escaped
first; only then are citation tags turned into markup this module writes
itself. Nothing from the memo is ever inserted unescaped.
"""
import re
from urllib.parse import quote, urlparse

from markupsafe import Markup, escape

from ..citations import TAG_RE

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
