"""Shared Jinja2 setup for every router's templates.

Timestamps are stored in UTC; the `localtime` filter converts them for display.
Window boundaries (limit days/months, admin date filters) are computed in
DISPLAY_TIMEZONE and converted to UTC, so the day a user reads here is the same
day their allowance is counted against — see limit_service.day_start_utc.

`static_url` versions static assets by content (/static/js/ui.js?v=<hash>), so a
browser holding an old copy fetches the new one as soon as the file changes.
"""
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings

DEFAULT_FORMAT = "%d %b %Y %H:%M"
STATIC_DIR = Path("frontend/static")

# path -> (mtime, short content hash); re-hashed only when the file changes.
_static_versions: dict[str, tuple[float, str]] = {}


def localtime(value: datetime | None, fmt: str = DEFAULT_FORMAT) -> str:
    """Render a UTC timestamp in DISPLAY_TIMEZONE, e.g. {{ t.created_at|localtime }}."""
    if value is None:
        return ""
    if value.tzinfo is None:  # naive values are UTC by convention in this app
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(settings.display_timezone)).strftime(fmt)


def static_url(path: str) -> str:
    """URL for a file under frontend/static, versioned by its content, e.g.
    {{ static_url("js/ui.js") }} -> /static/js/ui.js?v=3f9c1a2b7d."""
    file = STATIC_DIR / path
    try:
        mtime = file.stat().st_mtime
    except OSError:
        return f"/static/{path}"
    cached = _static_versions.get(path)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(file.read_bytes()).hexdigest()[:10])
        _static_versions[path] = cached
    return f"/static/{path}?v={cached[1]}"


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="frontend/templates")
    templates.env.filters["localtime"] = localtime
    templates.env.globals["explorer_tx_url"] = settings.xrpl_explorer_tx_url
    templates.env.globals["static_url"] = static_url
    return templates
