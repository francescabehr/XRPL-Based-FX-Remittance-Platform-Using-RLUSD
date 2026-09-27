"""Public pages (UI redesign Phase 5a): landing page at /, login and register."""
import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.remit_helpers import approved_sender, logged_in
from tests.test_shell import an_admin


def undecorated_icons(page: str) -> list[str]:
    """Bootstrap icons that are not aria-hidden (same rule as test_icons_are_decorative)."""
    return re.findall(r'<i class="bi[^>]*>', re.sub(r'<i class="bi[^>]*aria-hidden="true"[^>]*>', "", page))


# ── Landing ───────────────────────────────────────────────────────────────────

async def test_landing_renders_for_signed_out_visitors(client: AsyncClient):
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 200
    page = response.text
    assert 'href="/register"' in page and 'href="/login"' in page
    assert "Testnet" in page and "simulated" in page
    assert page.count("<h1") == 1
    assert not undecorated_icons(page)


async def test_landing_shows_no_pricing(client: AsyncClient):
    """Live rates and fees stay behind login and KYC (FR-FX-07)."""
    page = (await client.get("/")).text
    assert not re.search(r"\d\.\d{6} UCTUSD", page)  # no UCTUSD amount() on the page
    assert "$" not in page  # no USD amount() either
    assert "Exchange rate" not in page and "fee</" not in page.lower()


async def test_landing_redirects_signed_in_users(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        response = await client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"


async def test_landing_redirects_admins_to_overview(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        response = await client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"


# ── Auth screens keep their form contract ─────────────────────────────────────

async def test_login_form_contract(client: AsyncClient):
    page = (await client.get("/login")).text
    assert re.search(r'<form method="post" action="/login"', page)
    for field in ("email", "password"):
        assert f'name="{field}"' in page and f'id="{field}"' in page and f'for="{field}"' in page
    assert not undecorated_icons(page)


async def test_register_form_contract(client: AsyncClient):
    page = (await client.get("/register")).text
    assert re.search(r'<form method="post" action="/register"', page)
    for field in ("full_name", "email", "mobile", "password", "confirm_password"):
        assert f'name="{field}"' in page and f'id="{field}"' in page and f'for="{field}"' in page
    assert 'aria-describedby="password-help"' in page and 'id="password-help"' in page
    assert page.count("<h1") == 1
    assert not undecorated_icons(page)


async def test_auth_errors_render_in_the_new_layout(client: AsyncClient):
    login = await client.post("/login", data={"email": "noone@example.com", "password": "wrong"})
    assert login.status_code == 401
    assert 'role="alert"' in login.text and "Invalid email or password." in login.text

    register = await client.post("/register", data={
        "full_name": "Ivy", "email": "ivy@example.com", "mobile": "+27888888888",
        "password": "Secure123!", "confirm_password": "Different!",
    })
    assert register.status_code == 400
    assert 'role="alert"' in register.text and "Passwords do not match." in register.text
    assert 'value="ivy@example.com"' in register.text  # prefill kept


# ── Landing polish: globe, no em dashes in copy ───────────────────────────────

async def test_landing_globe_is_decorative(client: AsyncClient):
    page = (await client.get("/")).text
    svg = re.search(r'<svg class="ds-globe"[^>]*>', page).group(0)
    assert 'aria-hidden="true"' in svg
    assert page.count('class="ds-globe__route"') == 3
    assert page.count("<h1") == 1
    assert "<title>Orbyt · Send money across borders" in page


@pytest.mark.parametrize("path", ["/", "/login", "/about", "/legal/cookies"])  # last one is the 404 page
async def test_every_layout_links_the_orbyt_favicon(client: AsyncClient, path):
    client.cookies.clear()
    page = (await client.get(path, headers={"accept": "text/html"})).text
    assert re.search(r'<link rel="icon" type="image/svg\+xml" href="/static/img/favicon\.svg[?"]', page)
    assert '<span class="ds-wordmark__mark" aria-hidden="true"><i class="bi bi-arrow-left-right"' in page


async def test_register_flash_has_no_em_dash(client: AsyncClient):
    response = await client.post("/register", data={
        "full_name": "Jo Flash", "email": "jo.flash@example.com", "mobile": "+27811112222",
        "password": "Secure123!", "confirm_password": "Secure123!",
    })
    assert response.status_code == 302
    dashboard = (await client.get("/dashboard")).text  # the flash renders on the next page
    assert "Account created. Welcome!" in dashboard
    assert "Account created —" not in dashboard


def visible_text(page: str) -> str:
    """Page text a person reads: no scripts, styles, comments, tags or <title>."""
    page = re.sub(r"<(script|style|title)\b.*?</\1>", " ", page, flags=re.S)
    page = re.sub(r"<!--.*?-->", " ", page, flags=re.S)
    page = re.sub(r">\s*—\s*<", "><", page)  # a lone "—" is a no-value mark, not punctuation
    page = re.sub(r"<[^>]+>", " ", page)
    return re.sub(r"\s+", " ", page)


def sentence_em_dashes(page: str) -> list[str]:
    """Em dashes used as punctuation. A lone "—" (no value) and "— Select —" are fine."""
    text = visible_text(page).replace("— Select —", "")
    return [m.group(0) for m in re.finditer(r"\w[^—]{0,30} — [^—]{0,30}\w", text)]


async def test_no_em_dashes_in_page_copy(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    from tests.remit_helpers import remittance
    from tests.test_cashout import funded_recipient

    pages = {path: (await client.get(path)).text for path in ("/", "/login", "/register")}
    txn, sender, _ = await remittance(db)
    with logged_in(sender):
        for path in ("/dashboard", "/profile", "/beneficiaries", "/transactions", f"/transactions/{txn.id}"):
            pages[path] = (await client.get(path)).text
    recipient, _ = await funded_recipient(db)
    with logged_in(recipient):
        pages["/wallet"] = (await client.get("/wallet")).text
    with logged_in(await an_admin(db)):
        for path in ("/admin", "/admin/transactions", f"/admin/transactions/{txn.id}", "/admin/config"):
            pages[path] = (await client.get(path)).text
    for path, page in pages.items():
        assert not sentence_em_dashes(page), (path, sentence_em_dashes(page))
        titles = re.findall(r"<title>(.*?)</title>", page)
        assert titles and "—" not in titles[0], (path, titles)


# ── Rotating globe assets ─────────────────────────────────────────────────────

async def test_landing_loads_the_globe_scripts_with_a_fallback(client: AsyncClient):
    from app.templating import static_url

    page = (await client.get("/")).text
    for path in ("js/globe-land.js", "js/globe.js"):
        assert static_url(path) in page and "?v=" in static_url(path)
    canvas = re.search(r"<canvas[^>]*data-globe[^>]*>", page).group(0)
    assert "hidden" in canvas and 'aria-hidden="true"' in canvas
    assert re.search(r'<svg class="ds-globe" data-globe-fallback[^>]*aria-hidden="true"', page)


def test_globe_land_data_is_sane():
    import json
    from pathlib import Path

    text = Path("frontend/static/js/globe-land.js").read_text()
    flat = json.loads(re.search(r"window\.GLOBE_LAND=(\[.*\]);", text).group(1))
    points = list(zip(flat[::2], flat[1::2]))
    assert len(flat) % 2 == 0 and 1000 <= len(points) <= 6000
    assert all(-180 <= lon <= 180 and -90 <= lat <= 90 for lon, lat in points)
    # A dot near Johannesburg, none in the middle of the Atlantic.
    assert any(abs(lon - 28) < 2.5 and abs(lat + 26) < 2.5 for lon, lat in points)
    assert not any(abs(lon + 30) < 2.5 and abs(lat) < 2.5 for lon, lat in points)
