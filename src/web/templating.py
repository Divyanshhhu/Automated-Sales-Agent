"""The Jinja2 environment shared by every page module, with the display
helpers templates use.
"""
from pathlib import Path

from fastapi.templating import Jinja2Templates

from ..confidence import DISPLAY_NAMES, category_label, level_of
from ..leads import short_name
from .rendering import render_cited_text, safe_http_url

WEB_DIR = Path(__file__).resolve().parent

templates = Jinja2Templates(directory=str(WEB_DIR / "templates"))
templates.env.filters["cited"] = render_cited_text
templates.env.filters["safe_url"] = safe_http_url
# stored signal label -> level key ("strong", ...) and the words people read
templates.env.filters["signal_level"] = level_of
templates.env.filters["signal_name"] = lambda label: DISPLAY_NAMES[level_of(label)]
templates.env.filters["category"] = category_label
templates.env.filters["short"] = short_name
# Changes whenever a static file changes, so browsers fetch the new version
# instead of a cached one (links carry ?v=<this>).
templates.env.globals["asset_version"] = max(
    int(f.stat().st_mtime) for f in (WEB_DIR / "static").iterdir() if f.is_file()
)
templates.env.filters["money"] = lambda usd: f"${usd:,.2f}" if usd >= 0.01 or usd == 0 else "under $0.01"
