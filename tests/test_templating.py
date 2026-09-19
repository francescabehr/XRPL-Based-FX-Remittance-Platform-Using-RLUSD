"""Display-time conversion: stored UTC, shown in DISPLAY_TIMEZONE."""
from datetime import datetime, timezone

from app.config import settings
from app.templating import localtime


def test_utc_is_shown_in_display_timezone(monkeypatch):
    monkeypatch.setattr(settings, "display_timezone", "Africa/Johannesburg")
    assert localtime(datetime(2026, 9, 19, 18, 12, tzinfo=timezone.utc)) == "19 Sep 2026 20:12"


def test_naive_values_are_treated_as_utc(monkeypatch):
    monkeypatch.setattr(settings, "display_timezone", "Africa/Johannesburg")
    assert localtime(datetime(2026, 9, 19, 23, 30), "%d %b %H:%M") == "20 Sep 01:30"


def test_none_renders_empty():
    assert localtime(None) == ""
