"""Phase 4a: onboarding checklist, role-aware dashboard (audit §4.4), KYC form."""
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import KYCStatus
from tests.remit_helpers import approved_sender, beneficiary_for, logged_in, registered_recipient
from tests.test_cashout import funded_recipient


async def _dashboard(client: AsyncClient, user) -> str:
    with logged_in(user):
        response = await client.get("/dashboard")
    assert response.status_code == 200
    return response.text


async def test_unverified_sender_sees_the_checklist(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)            # can_send, KYC not submitted, no wallet
    page = await _dashboard(client, user)
    assert "Let's get you started" in page
    assert "0 of 3 done" in page
    assert 'href="/kyc"' in page and "Start KYC" in page
    assert "Sending is disabled until your KYC is approved" in page
    assert "Available to send today" not in page
    assert 'disabled aria-describedby="kyc-status-note"' in page and 'id="kyc-status-note"' in page


async def test_pending_kyc_explains_the_wait(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    user.kyc_status = KYCStatus.pending
    await db.commit()
    page = await _dashboard(client, user)
    assert "1 of 3 done" in page
    assert "Awaiting approval" in page                # the status_badge label


async def test_approved_sender_without_recipient_sees_checklist_and_hero(client: AsyncClient, db: AsyncSession, seed_tiers):
    page = await _dashboard(client, await approved_sender(db))
    assert "2 of 3 done" in page
    assert 'href="/beneficiaries/new"' in page
    assert "Available to send today" in page          # approved: the hero is live already


async def test_set_up_sender_sees_home_without_checklist(client: AsyncClient, db: AsyncSession, seed_tiers):
    sender = await approved_sender(db)
    await beneficiary_for(db, sender, await registered_recipient(db))
    page = await _dashboard(client, sender)
    assert "Let's get you started" not in page
    assert "Available to send today" in page
    assert "R10,000.00" in page                        # daily limit, via amount()
    assert "Used today" in page and "Used this month" in page
    assert "Recent activity" in page and "No transfers yet" in page
    assert "Wallet balance" not in page


async def test_recipient_only_sees_wallet_not_sending(client: AsyncClient, db: AsyncSession, seed_tiers):
    user, _ = await funded_recipient(db, balance="51.669316")
    user.can_send = False
    await db.commit()
    page = await _dashboard(client, user)
    assert "Wallet balance" in page and "51.669316 UCTUSD" in page
    assert 'href="/cashout"' in page
    assert "Available to send today" not in page
    assert "Let's get you started" not in page
    assert "Send Money" not in page


async def test_dual_role_user_sees_both(client: AsyncClient, db: AsyncSession, seed_tiers):
    user, _ = await funded_recipient(db)
    user.kyc_status = KYCStatus.approved
    await db.commit()
    page = await _dashboard(client, user)
    assert "Wallet balance" in page
    assert "Available to send today" in page
    assert page.index("Wallet balance") < page.index("Available to send today")


async def test_kyc_form_is_grouped_and_keeps_its_fields(client: AsyncClient, db: AsyncSession):
    with logged_in(await registered_recipient(db)):
        page = (await client.get("/kyc")).text
    for legend in ["Personal details", "Address", "Contact", "Source of funds"]:
        assert f">{legend}</legend>" in page
    for name in ["full_name", "date_of_birth", "nationality", "id_number", "residential_address",
                 "mobile", "email", "source_of_funds"]:
        assert f'name="{name}"' in page and f'id="kyc-{name}"' in page
    assert 'action="/kyc"' in page
