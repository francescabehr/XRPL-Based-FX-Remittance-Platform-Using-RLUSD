"""Shared Jinja2 setup for every router's templates.

Timestamps are stored and computed in UTC (limits reset at UTC midnight); the
`localtime` filter converts them for display only.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from app.config import settings

DEFAULT_FORMAT = "%d %b %Y %H:%M"


def localtime(value: datetime | None, fmt: str = DEFAULT_FORMAT) -> str:
    """Render a UTC timestamp in DISPLAY_TIMEZONE, e.g. {{ t.created_at|localtime }}."""
    if value is None:
        return ""
    if value.tzinfo is None:  # naive values are UTC by convention in this app
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(settings.display_timezone)).strftime(fmt)


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="frontend/templates")
    templates.env.filters["localtime"] = localtime
    templates.env.globals["explorer_tx_url"] = settings.xrpl_explorer_tx_url
    return templates
