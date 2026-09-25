"""App shell (UI redesign Phase 3): sidebar, top bar, admin overview, a11y fixes."""
import re
import uuid
from datetime import date, datetime, timezone

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import CashInStatus, SettlementStatus
from app.services import cashin_service, cashout_service
from app.services.auth_service import create_user
from app.services.kyc_service import count_pending_submissions, submit_kyc
from app.services.limit_service import day_start_utc, display_tz
from tests.remit_helpers import approved_sender, logged_in, registered_recipient, remittance
from tests.test_cashout import a_request, funded_recipient


async def an_admin(db: AsyncSession):
    t = uuid.uuid4().hex[:8]
    return await create_user(
        db, full_name=f"Admin {t}", email=f"adm{t}@test.com",
        mobile=f"+2776{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}", password="Pass1234!", is_admin=True,
    )


def active_link(html: str) -> str:
    """Label of the sidebar item marked aria-current="page"."""
    match = re.search(r'<a class="ds-sidebar__link is-active"[^>]*aria-current="page">.*?<span>(.*?)</span>', html, re.S)
    return match.group(1) if match else ""


# ── Admin landing ─────────────────────────────────────────────────────────────

async def test_admin_login_lands_on_overview(client: AsyncClient, db: AsyncSession):
    admin = await an_admin(db)
    response = await client.post("/login", data={"email": admin.email, "password": "Pass1234!"}, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/admin"

    with logged_in(admin):
        again = await client.get("/login", follow_redirects=False)
    assert again.headers["location"] == "/admin"


async def test_overview_renders_all_tiles(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        response = await client.get("/admin")
    assert response.status_code == 200
    for label in ["Pending KYC", "Cash-ins awaiting confirmation", "Pending cash-outs",
                  "Failed settlements", "Settled on XRPL today"]:
        assert label in response.text
    assert active_link(response.text) == "Overview"


async def _counts(db: AsyncSession) -> dict:
    today = day_start_utc(datetime.now(display_tz()).date())
    cashouts = await cashout_service.count_open(db)
    return {
        "kyc": await count_pending_submissions(db),
        "cashin": await cashin_service.count_pending_cashins(db),
        "cashout": cashouts["requested"],
        "failed": await cashin_service.count_failed_settlements(db),
        "today": await cashin_service.count_settled_since(db, today),
    }


async def test_overview_counts_track_the_queues(db: AsyncSession, seed_tiers, seed_fee_config):
    """Counts are compared as deltas: the test DB is shared across tests."""
    before = await _counts(db)

    user = await registered_recipient(db)
    await submit_kyc(
        db, user, full_name=user.full_name, date_of_birth=date(1990, 1, 1), nationality="South African",
        id_number="9001010001087", residential_address="1 Main St", mobile=user.mobile, email=user.email,
        source_of_funds="Salary",
    )
    pending, _, _ = await remittance(db)                     # a pending cash-in
    failed, _, _ = await remittance(db)
    failed.cashin_status = CashInStatus.received
    failed.settlement_status = SettlementStatus.failed
    settled, _, _ = await remittance(db)
    settled.cashin_status = CashInStatus.received
    settled.settlement_status = SettlementStatus.completed
    settled.settled_at = datetime.now(timezone.utc)
    await db.commit()
    await a_request(db)                                      # a requested cash-out

    after = await _counts(db)
    assert after["kyc"] == before["kyc"] + 1
    assert after["cashin"] == before["cashin"] + 1           # failed/settled ones were received
    assert after["failed"] == before["failed"] + 1
    assert after["today"] == before["today"] + 1
    assert after["cashout"] == before["cashout"] + 1
    assert pending.cashin_status == CashInStatus.pending


# ── Role-aware sidebar ────────────────────────────────────────────────────────

async def test_sender_sidebar_has_send_links_but_no_wallet(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/dashboard")).text
    assert 'href="/send"' in page and 'href="/beneficiaries"' in page
    assert 'href="/wallet"' not in page and 'href="/cashout/history"' not in page
    assert 'href="/admin' not in page
    assert active_link(page) == "Home"


async def test_recipient_sidebar_has_wallet_links(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db)
    with logged_in(user):
        page = (await client.get("/cashout/history")).text
    for href in ['href="/wallet"', 'href="/cashout"', 'href="/cashout/history"']:
        assert href in page
    assert active_link(page) == "My cash-outs"


async def test_admin_sidebar_groups(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/cashin")).text
    for group in ["Queues", "Monitor", "Config"]:
        assert f">{group}</p>" in page
    assert 'href="/send"' not in page
    assert active_link(page) == "Cash-in"


# ── Shell ─────────────────────────────────────────────────────────────────────

async def test_shell_replaces_the_dark_navbar(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/dashboard")).text
    assert "navbar-dark" not in page
    assert 'href="#main"' in page and 'id="main"' in page
    assert 'id="ds-sidebar"' in page and 'data-bs-target="#ds-sidebar"' in page
    assert 'class="ds-topbar__kyc"' in page                   # KYC badge for non-admins
    assert 'class="alert alert- ' not in page


async def test_admin_top_bar_has_no_kyc_badge(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin")).text
    assert "ds-topbar__kyc" not in page


async def test_flash_uses_the_design_system(client: AsyncClient, db: AsyncSession):
    unapproved = await create_user(
        db, full_name="Pending Person", email=f"p{uuid.uuid4().hex[:8]}@test.com",
        mobile=f"+2771{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}", password="Pass1234!",
    )
    with logged_in(unapproved):
        page = (await client.get("/send", follow_redirects=True)).text
    assert "ds-flash" in page and "KYC must be approved" in page


# ── Accessibility ─────────────────────────────────────────────────────────────

async def test_login_labels_are_tied_to_inputs(client: AsyncClient):
    page = (await client.get("/login")).text
    assert 'for="email"' in page and 'id="email"' in page
    assert 'for="password"' in page and 'id="password"' in page


async def test_icons_are_decorative(client: AsyncClient, db: AsyncSession):
    pages = [(await client.get("/login")).text]
    with logged_in(await approved_sender(db)):
        pages.append((await client.get("/dashboard")).text)
    with logged_in(await an_admin(db)):
        pages.append((await client.get("/admin")).text)
    for page in pages:
        assert not re.search(r'<i class="bi[^>]*>', re.sub(r'<i class="bi[^>]*aria-hidden="true"[^>]*>', "", page))


async def test_irreversible_admin_actions_ask_for_confirmation(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    txn, _, _ = await remittance(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/cashin")).text
    for action in ("received", "failed"):
        form = re.search(rf'<form[^>]*action="/admin/cashin/{txn.id}/{action}"[^>]*>', page, re.S)
        assert form and "data-confirm=" in form.group(0)
