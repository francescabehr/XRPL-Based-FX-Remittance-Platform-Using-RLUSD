"""Phase 4e: admin overview, queues, detail pages and config."""
import re
from datetime import date, datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cashout import CashOutStatus
from app.models.transaction import CashInStatus, SettlementStatus
from app.services.kyc_service import approve_kyc, reject_kyc, submit_kyc
from tests.remit_helpers import logged_in, registered_recipient, remittance
from tests.test_cashout import a_request, an_admin

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")

HASH = "E" * 64


async def _submission(db):
    user = await registered_recipient(db)
    return await submit_kyc(
        db, user, full_name=user.full_name, date_of_birth=date(1990, 1, 1), nationality="South African",
        id_number="9001015009087", residential_address="1 Main Rd, Cape Town", mobile=user.mobile,
        email=user.email, source_of_funds="Salary",
    )


async def _failed_settlement(db, reason="tecPATH_DRY: no trust line"):
    txn, _, _ = await remittance(db)
    txn.cashin_status, txn.settlement_status, txn.xrpl_error_reason = CashInStatus.received, SettlementStatus.failed, reason
    txn.xrpl_tx_hash = HASH
    await db.commit()
    return txn


# --- Overview ------------------------------------------------------------------------

async def test_overview_lists_failed_payments(client: AsyncClient, db: AsyncSession):
    txn = await _failed_settlement(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin")).text
    assert "Failed payments" in page
    assert f'href="/admin/transactions/{txn.id}"' in page
    assert "tecPATH_DRY: no trust line" in page and "ds-status--danger" in page


# --- KYC ------------------------------------------------------------------------------

async def test_kyc_queue_has_review_links_and_no_decisions(client: AsyncClient, db: AsyncSession):
    sub = await _submission(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/kyc")).text
    assert f'href="/admin/kyc/{sub.id}"' in page and "Review" in page
    assert "Awaiting approval" in page
    # KYC is decided on the detail page only — never inline in the queue.
    assert "/approve" not in page and "/reject" not in page


async def test_kyc_detail_shows_the_decision_forms_while_pending(client: AsyncClient, db: AsyncSession):
    sub = await _submission(db)
    with logged_in(await an_admin(db)):
        page = (await client.get(f"/admin/kyc/{sub.id}")).text
    for action in ("approve", "reject"):
        form = re.search(rf'<form[^>]*action="/admin/kyc/{sub.id}/{action}"[^>]*>', page, re.S)
        assert form and "data-confirm=" in form.group(0)


@pytest.mark.parametrize("decision", ["approved", "rejected"])
async def test_kyc_detail_shows_the_outcome_once_decided(client: AsyncClient, db: AsyncSession, decision):
    sub = await _submission(db)
    admin = await an_admin(db)
    if decision == "approved":
        await approve_kyc(db, sub, admin)
    else:
        await reject_kyc(db, sub, admin, "Blurry ID photo")
    with logged_in(admin):
        page = (await client.get(f"/admin/kyc/{sub.id}")).text
    assert f"/admin/kyc/{sub.id}/approve" not in page and f"/admin/kyc/{sub.id}/reject" not in page
    assert admin.full_name in page
    assert ("Verified" if decision == "approved" else "Blurry ID photo") in page


# --- Cash-in --------------------------------------------------------------------------

async def test_cashin_queue_uses_shared_formats(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/cashin")).text
    assert "R1,000.00" in page and "50.874404 UCTUSD" in page
    assert "Awaiting payment" in page
    assert f'href="/admin/transactions/{txn.id}"' in page and f'id="fail-{txn.id}"' in page
    assert re.search(rf"/admin/cashin/{txn.id}/received", page)          # perf harness regex


async def test_transaction_detail_offers_cashin_actions_only_while_pending(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db)
    admin = await an_admin(db)
    with logged_in(admin):
        pending = (await client.get(f"/admin/transactions/{txn.id}")).text
    assert f'action="/admin/cashin/{txn.id}/received"' in pending and f'action="/admin/cashin/{txn.id}/failed"' in pending

    txn.cashin_status, txn.settlement_status = CashInStatus.received, SettlementStatus.completed
    await db.commit()
    with logged_in(admin):
        done = (await client.get(f"/admin/transactions/{txn.id}")).text
    assert "/admin/cashin/" not in done and "/retry" not in done
    assert "Settled on XRPL" in done and "Payment received" in done


async def test_transaction_detail_offers_retry_for_a_failed_or_stuck_settlement(client: AsyncClient, db: AsyncSession):
    failed = await _failed_settlement(db)
    stuck, _, _ = await remittance(db)
    stuck.cashin_status, stuck.settlement_status = CashInStatus.received, SettlementStatus.processing
    stuck.updated_at = datetime.now(timezone.utc) - timedelta(minutes=30)
    await db.commit()
    with logged_in(await an_admin(db)):
        failed_page = (await client.get(f"/admin/transactions/{failed.id}")).text
        stuck_page = (await client.get(f"/admin/transactions/{stuck.id}")).text
    assert f'action="/admin/settlements/{failed.id}/retry"' in failed_page and ">Retry<" in failed_page.replace("</i>", "")
    assert f'action="/admin/settlements/{stuck.id}/retry"' in stuck_page and "Recover" in stuck_page


# --- Settlements ----------------------------------------------------------------------

async def test_settlement_monitor_uses_badges_and_links_to_the_drill_down(client: AsyncClient, db: AsyncSession):
    txn = await _failed_settlement(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/settlements")).text
    assert f'href="/admin/transactions/{txn.id}"' in page
    assert f'href="/transactions/{txn.id}"' not in page
    assert "ds-status--danger" in page and f"https://testnet.xrpl.org/transactions/{HASH}" in page


# --- Cash-out -------------------------------------------------------------------------

async def test_cashout_queue_uses_shared_badges(client: AsyncClient, db: AsyncSession):
    req, _, _ = await a_request(db, balance="100", amount="10")
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/cashout")).text
    assert "Cash-out queue" in page and f'href="/admin/cashout/{req.id}"' in page
    assert re.search(r'id="reject\d+"', page) and "Requested" in page


async def test_admin_cashout_detail_shows_the_burn_metadata(client: AsyncClient, db: AsyncSession):
    req, user, _ = await a_request(db, balance="100", amount="10")
    req.status, req.approved_at = CashOutStatus.approved, datetime.now(timezone.utc)
    req.xrpl_burn_tx_hash, req.failure_reason = HASH, "outcome_unknown: ledger timeout"
    req.burn_submitted_ledger_index, req.burn_last_ledger_sequence, req.burn_attempts = 1000, 1020, 1
    await db.commit()
    admin = await an_admin(db)
    with logged_in(admin):
        page = (await client.get(f"/admin/cashout/{req.id}")).text
    assert "Awaiting ledger confirmation" in page and "outcome_unknown: ledger timeout" in page
    assert "1000–1020" in page and f"https://testnet.xrpl.org/transactions/{HASH}" in page
    assert f'action="/admin/cashout/{req.id}/reconcile"' in page
    assert f"/admin/cashout/{req.id}/approve" not in page
    with logged_in(user):
        assert (await client.get(f"/admin/cashout/{req.id}", follow_redirects=False)).status_code == 403


async def test_admin_cashout_detail_actions_follow_the_state(client: AsyncClient, db: AsyncSession):
    req, _, _ = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)
    with logged_in(admin):
        requested = (await client.get(f"/admin/cashout/{req.id}")).text
    assert f'action="/admin/cashout/{req.id}/approve"' in requested and f'action="/admin/cashout/{req.id}/reject"' in requested

    req.status = CashOutStatus.completed
    await db.commit()
    with logged_in(admin):
        completed = (await client.get(f"/admin/cashout/{req.id}")).text
    assert "/approve" not in completed and "/reconcile" not in completed and "No further action" in completed


async def test_admin_cashout_detail_unknown_id_redirects(client: AsyncClient, db: AsyncSession):
    import uuid
    with logged_in(await an_admin(db)):
        r = await client.get(f"/admin/cashout/{uuid.uuid4()}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/admin/cashout"


# --- Config ---------------------------------------------------------------------------

async def test_config_tier_forms_are_valid_html(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/config")).text
    assert not re.search(r"<tr>\s*<form", page)                     # no <form> straight inside a <tr>
    forms = re.findall(r'<form method="post" action="/admin/config/tiers/([0-9a-f-]{36})" id="tier-\1">', page)
    assert forms
    for tier_id in forms:
        assert page.count(f'form="tier-{tier_id}"') == 2            # daily + monthly inputs join it
    assert 'action="/admin/config/fees"' in page
    for name in ("fixed_fee_zar", "percentage_fee", "fx_margin", "cashout_fee_percentage",
                 "cashout_fee_min_usd", "min_send_zar", "market_rate_zar_per_usd"):
        assert f'name="{name}"' in page and f'id="cfg-{name}"' in page
