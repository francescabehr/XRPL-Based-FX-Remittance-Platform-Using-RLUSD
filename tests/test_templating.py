"""Display-time conversion (stored UTC, shown in DISPLAY_TIMEZONE) and static asset versioning."""
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path

from app import templating
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


# ── Cache-busting: static assets are versioned by content ─────────────────────

ASSETS = ["css/style.css", "css/design-system.css", "js/ui.js"]


def _content_version(path: str) -> str:
    return hashlib.sha256((Path("frontend/static") / path).read_bytes()).hexdigest()[:10]


async def test_base_renders_versioned_asset_urls(client):
    page = (await client.get("/login")).text
    for path in ASSETS:
        assert f"/static/{path}?v={_content_version(path)}" in page
        assert f'"/static/{path}"' not in page  # never the bare, cacheable URL


def test_version_changes_when_the_file_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(templating, "STATIC_DIR", tmp_path)
    monkeypatch.setattr(templating, "_static_versions", {})
    asset = tmp_path / "app.js"
    asset.write_text("one")
    first = templating.static_url("app.js")
    asset.write_text("two")
    os.utime(asset, (asset.stat().st_atime, asset.stat().st_mtime + 5))
    second = templating.static_url("app.js")
    assert first.startswith("/static/app.js?v=") and first != second
    assert templating.static_url("missing.js") == "/static/missing.js"
