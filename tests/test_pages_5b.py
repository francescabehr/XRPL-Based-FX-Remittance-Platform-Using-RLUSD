"""UI redesign Phase 5b: /profile (read-only) and the beneficiaries list and form."""
import re

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import KYCStatus
from app.services.beneficiary_service import create_beneficiary
from tests.remit_helpers import approved_sender, logged_in, registered_recipient
from tests.test_cashout import funded_recipient
from tests.test_public_pages import undecorated_icons
from tests.test_shell import active_link, an_admin


async def a_beneficiary(db: AsyncSession, sender, **overrides):
    fields = dict(full_name="Thandi Mokoena", email=None, mobile="+27820000001", country="South Africa",
                  payout_currency="USD", relationship="Sibling")
    fields.update(overrides)
    return await create_beneficiary(db, sender, **fields)


# ── Profile ───────────────────────────────────────────────────────────────────

async def test_profile_requires_login(client: AsyncClient):
    response = await client.get("/profile", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_profile_for_a_sender(client: AsyncClient, db: AsyncSession, seed_tiers):
    sender = await approved_sender(db)
    with logged_in(sender):
        page = (await client.get("/profile")).text
    assert sender.full_name in page and sender.email in page and sender.mobile in page
    assert "Verified" in page and "View submission" in page
    assert "Used today" in page and "Used this month" in page and "Standard tier" in page
    assert "XRPL wallet" not in page
    # No "Profile" nav item: the footer's user block is the link, marked current here.
    assert active_link(page) == ""
    assert '<a class="ds-sidebar__profile is-active" href="/profile" aria-current="page">' in page
    assert page.count("<h1") == 1
    assert not undecorated_icons(page)


async def test_sidebar_user_block_links_to_profile(client: AsyncClient, db: AsyncSession, seed_tiers):
    sender = await approved_sender(db)
    with logged_in(sender):
        page = (await client.get("/dashboard")).text
    assert '<a class="ds-sidebar__profile" href="/profile">' in page
    assert 'aria-current="page">' not in page.split('class="ds-sidebar__foot"')[1]
    assert '<span>Profile</span>' not in page
    assert 'href="/logout"' in page


async def test_profile_asks_unverified_users_to_verify(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    assert user.kyc_status == KYCStatus.not_submitted
    with logged_in(user):
        page = (await client.get("/profile")).text
    assert "Not started" in page and 'href="/kyc"' in page and "Start verification" in page


async def test_profile_shows_the_wallet_address_but_no_secrets(client: AsyncClient, db: AsyncSession, seed_tiers):
    user, wallet = await funded_recipient(db)
    wallet.encrypted_private_key = "gAAAAA-preview-sealed-seed-value"
    wallet.key_encryption_key_id = "kid-fingerprint-0042"
    await db.commit()
    with logged_in(user):
        page = (await client.get("/profile")).text
    assert wallet.xrpl_address in page
    assert f"/accounts/{wallet.xrpl_address}" in page
    assert f'data-copy="{wallet.xrpl_address}"' in page
    assert "Set up to the UCTUSD issuer" in page
    assert "gAAAAA-preview-sealed-seed-value" not in page
    assert "kid-fingerprint-0042" not in page
    assert "encrypted" not in page.lower() and "seed" not in page.lower()


async def test_profile_for_an_admin(client: AsyncClient, db: AsyncSession):
    admin = await an_admin(db)
    with logged_in(admin):
        page = (await client.get("/profile")).text
    assert "Administrator" in page and admin.email in page
    assert "Verification" not in page.split('<main')[1]
    assert "Sending limits" not in page and "XRPL wallet" not in page


async def test_sidebar_footer_links_to_profile(client: AsyncClient, db: AsyncSession, seed_tiers):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/dashboard")).text
    assert '<a class="ds-sidebar__profile" href="/profile">' in page


# ── Beneficiaries ─────────────────────────────────────────────────────────────

async def test_beneficiaries_empty_state(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/beneficiaries")).text
    assert "ds-empty-state" in page and "No beneficiaries yet" in page
    assert page.count('href="/beneficiaries/new"') >= 2  # top bar + empty-state CTA


async def test_beneficiary_cards(client: AsyncClient, db: AsyncSession):
    sender = await approved_sender(db)
    recipient = await registered_recipient(db)
    linked = await a_beneficiary(db, sender, full_name="Linked Person", email=recipient.email, mobile=None)
    unlinked = await a_beneficiary(db, sender, full_name="New Person", payout_currency="ZAR")
    with logged_in(sender):
        page = (await client.get("/beneficiaries")).text

    assert "Linked" in page and "Not registered" in page and "ZAR payout" in page
    for ben in (linked, unlinked):
        form = re.search(rf'<form method="post" action="/beneficiaries/{ben.id}/delete"[^>]*>', page, re.S).group(0)
        assert "data-confirm=" in form and 'data-confirm-label="Remove"' in form
        assert f'href="/beneficiaries/{ben.id}/edit"' in page
        assert f'href="/send?beneficiary_id={ben.id}"' in page
    assert page.count("<h1") == 1
    assert not undecorated_icons(page)


async def test_send_shortcut_needs_approved_kyc(client: AsyncClient, db: AsyncSession):
    sender = await approved_sender(db)
    sender.kyc_status = KYCStatus.pending
    await db.commit()
    ben = await a_beneficiary(db, sender)
    with logged_in(sender):
        page = (await client.get("/beneficiaries")).text
    assert f'href="/send?beneficiary_id={ben.id}"' not in page
    assert f'href="/beneficiaries/{ben.id}/edit"' in page


async def _form_contract(page: str, action: str):
    assert re.search(rf'<form method="post" action="{re.escape(action)}" novalidate>', page)
    for name, field_id in [("full_name", "ben-full_name"), ("relationship", "ben-relationship"),
                           ("email", "ben-email"), ("mobile", "ben-mobile"), ("country", "ben-country")]:
        assert f'name="{name}"' in page and f'id="{field_id}"' in page and f'for="{field_id}"' in page
    for cur in ("USD", "ZAR"):
        assert re.search(rf'<input[^>]*name="payout_currency"[^>]*id="cur_{cur}" value="{cur}"', page, re.S)
        assert f'for="cur_{cur}"' in page
    assert page.count("<h1") == 1
    assert not undecorated_icons(page)


async def test_new_beneficiary_form_contract(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/beneficiaries/new")).text
    await _form_contract(page, "/beneficiaries/new")
    assert "Add Beneficiary" in page
    assert re.search(r'id="cur_USD" value="USD"\s+checked', page)  # USD default kept


async def test_edit_beneficiary_form_contract(client: AsyncClient, db: AsyncSession):
    sender = await approved_sender(db)
    ben = await a_beneficiary(db, sender, payout_currency="ZAR")
    with logged_in(sender):
        page = (await client.get(f"/beneficiaries/{ben.id}/edit")).text
    await _form_contract(page, f"/beneficiaries/{ben.id}/edit")
    assert "Save Changes" in page and 'value="Thandi Mokoena"' in page
    assert re.search(r'id="cur_ZAR" value="ZAR"\s+checked', page)


async def test_beneficiary_form_error(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        response = await client.post("/beneficiaries/new", data={
            "full_name": "No Contact", "email": "", "mobile": "", "country": "Kenya",
            "payout_currency": "ZAR", "relationship": "Friend",
        })
    assert response.status_code == 400
    assert 'role="alert"' in response.text and "At least one of email or mobile is required." in response.text
    assert 'value="No Contact"' in response.text
