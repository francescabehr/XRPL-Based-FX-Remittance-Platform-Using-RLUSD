"""UI redesign Phase 5d: HTML 403/404 pages for browsers, JSON everywhere else; config empty states."""
from httpx import AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.platform_config import FeeConfig, LimitTier
from tests.remit_helpers import approved_sender, logged_in, remittance
from tests.test_public_pages import undecorated_icons
from tests.test_shell import an_admin

HTML = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}


# ── 404 ───────────────────────────────────────────────────────────────────────

async def test_404_page_for_signed_out_browsers(client: AsyncClient):
    response = await client.get("/no-such-page", headers=HTML)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert "Page not found" in page and "Error 404" in page
    assert 'href="/"' in page and "Go to the home page" in page
    assert "ds-shell" not in page and "ds-public-header" in page
    assert page.count("<h1") == 1 and not undecorated_icons(page)


async def test_404_page_inside_the_shell_when_signed_in(client: AsyncClient, db: AsyncSession, seed_tiers):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/no-such-page", headers=HTML)).text
    assert "ds-shell" in page and "ds-sidebar" in page
    assert "Page not found" in page and 'href="/dashboard"' in page and "Go to your dashboard" in page
    assert page.count("<h1") == 1 and not undecorated_icons(page)


async def test_404_stays_json_without_an_html_accept(client: AsyncClient):
    response = await client.get("/no-such-page")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


# ── 403 ───────────────────────────────────────────────────────────────────────

async def test_403_page_for_a_non_admin_on_an_admin_page(client: AsyncClient, db: AsyncSession, seed_tiers):
    with logged_in(await approved_sender(db)):
        response = await client.get("/admin/transactions", headers=HTML, follow_redirects=False)
    assert response.status_code == 403
    page = response.text
    assert "have access to this page" in page and "Error 403" in page
    assert "ds-sidebar" in page and 'href="/dashboard"' in page
    assert "Admin access required." not in page  # internal detail never shown


async def test_403_on_an_admin_form_post_is_html_too(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    txn, sender, _ = await remittance(db)
    with logged_in(sender):
        response = await client.post(f"/admin/cashin/{txn.id}/received", headers=HTML, follow_redirects=False)
    assert response.status_code == 403 and "have access to this page" in response.text


async def test_403_stays_json_without_an_html_accept(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        response = await client.get("/admin/transactions", follow_redirects=False)
    assert response.status_code == 403
    assert response.json() == {"detail": "Admin access required."}


async def test_json_apis_keep_json_errors_even_for_browsers(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    txn, sender, _ = await remittance(db)
    with logged_in(sender):
        quote = await client.get("/quote", params={"beneficiary_id": str(txn.id), "zar_amount": "100"}, headers=HTML)
        patch = await client.patch(f"/transactions/{txn.id}/cashin", json={"status": "received"}, headers=HTML)
    assert quote.status_code == 404 and quote.json()["detail"] == "Beneficiary not found."
    assert patch.status_code == 403 and patch.json() == {"detail": "Admin access required."}


async def test_signed_out_admin_page_still_redirects_to_login(client: AsyncClient):
    response = await client.get("/admin/transactions", headers=HTML, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/login"


# ── Config empty states ───────────────────────────────────────────────────────

async def test_config_empty_states(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    admin = await an_admin(db)  # create_user commits — do it before the (uncommitted) deletes
    await db.execute(delete(LimitTier))
    await db.execute(delete(FeeConfig))
    await db.flush()  # never committed: the db fixture rolls it back
    with logged_in(admin):
        page = (await client.get("/admin/config")).text
    assert "No limit tiers configured" in page and "No fee configuration" in page
    assert page.count("ds-empty-state__icon") == 2
