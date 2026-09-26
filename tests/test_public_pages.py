"""Public pages (UI redesign Phase 5a): landing page at /, login and register."""
import re

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
