"""FR-CI-01..05  Remittance creation, simulated card cash-in, settlement gating."""
import asyncio
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.models.transaction import CashInStatus, SettlementStatus, Transaction
from app.models.user import User
from app.services import cashin_service, queue_service
from app.services.cashin_service import MockCard, RemittanceError, create_remittance
from app.services.limit_service import get_daily_usage
from tests.conftest import _TestSession
from tests.remit_helpers import (
    DECLINED_CARD,
    GOOD_CARD,
    RecordingEnqueue,
    approved_sender,
    beneficiary_for,
    logged_in,
    registered_recipient,
    remittance,
)

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")


async def _count(db, sender_id):
    return (await db.execute(select(func.count()).where(Transaction.sender_id == sender_id))).scalar_one()


# --- creation (FR-CI-01, FR-LIM) ---

async def test_create_remittance_snapshots_quote_and_starts_pending(db):
    txn, sender, recipient = await remittance(db, "1000")

    assert txn.cashin_status == CashInStatus.pending
    assert txn.settlement_status == SettlementStatus.not_queued
    assert txn.recipient_user_id == recipient.id
    assert txn.uctusd_amount == Decimal("50.874404")  # the Phase 4 worked example
    assert txn.card_last4 == "4242"
    assert txn.idempotency_key is not None


async def test_only_last4_of_card_is_stored(db):
    txn, _, _ = await remittance(db)
    columns = {c.key: getattr(txn, c.key) for c in Transaction.__table__.columns}
    assert not any("4242424242424242" in str(v).replace(" ", "") for v in columns.values())


async def test_unregistered_recipient_is_refused(db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, recipient=None)

    with pytest.raises(RemittanceError, match="does not have an account"):
        await create_remittance(db, sender, beneficiary_id=ben.id, zar_amount=Decimal("100"), card=GOOD_CARD)
    assert await _count(db, sender.id) == 0


async def test_recipient_who_registers_later_is_linked_at_send_time(db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, recipient=None, email="latecomer_ci@test.com")
    assert ben.recipient_user_id is None

    from app.services.auth_service import create_user
    late = await create_user(db, full_name="Late Comer", email="latecomer_ci@test.com",
                             mobile="+27795550001", password="Pass1234!")

    txn = await create_remittance(db, sender, beneficiary_id=ben.id, zar_amount=Decimal("100"), card=GOOD_CARD)
    assert txn.recipient_user_id == late.id
    await db.refresh(ben)
    assert ben.recipient_user_id == late.id


async def test_unapproved_sender_is_refused(db):
    sender = await approved_sender(db)
    recipient = await registered_recipient(db)
    ben = await beneficiary_for(db, sender, recipient)
    from app.models.user import KYCStatus
    sender.kyc_status = KYCStatus.pending
    await db.commit()

    with pytest.raises(RemittanceError, match="KYC"):
        await create_remittance(db, sender, beneficiary_id=ben.id, zar_amount=Decimal("100"), card=GOOD_CARD)


async def test_over_limit_is_refused_before_insert(db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))

    with pytest.raises(RemittanceError, match="Daily limit exceeded"):
        await create_remittance(db, sender, beneficiary_id=ben.id, zar_amount=Decimal("10000.01"), card=GOOD_CARD)
    assert await _count(db, sender.id) == 0


async def test_changed_rate_is_refused(db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))

    with pytest.raises(RemittanceError, match="exchange rate changed"):
        await create_remittance(
            db, sender, beneficiary_id=ben.id, zar_amount=Decimal("100"), card=GOOD_CARD,
            expected_exchange_rate=Decimal("17.000000"),
        )


async def test_concurrent_sends_cannot_both_use_the_same_headroom(db):
    """Two R6,000 sends against a R10,000 daily limit: exactly one succeeds (TOCTOU)."""
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))

    async def attempt():
        async with _TestSession() as s:
            me = await s.get(User, sender.id)
            try:
                await create_remittance(s, me, beneficiary_id=ben.id, zar_amount=Decimal("6000"), card=GOOD_CARD)
                return "ok"
            except RemittanceError as exc:
                return str(exc)

    results = await asyncio.gather(attempt(), attempt())
    assert results.count("ok") == 1
    assert any("Daily limit exceeded" in r for r in results)
    assert await get_daily_usage(db, sender.id) == Decimal("6000")


@pytest.mark.parametrize(
    "card, message",
    [
        (MockCard("4242 4242 4242 4241", "12/30", "123", "X"), "not valid"),
        (MockCard("4242 4242 4242 4242", "01/20", "123", "X"), "expired"),
        (MockCard("4242 4242 4242 4242", "13/30", "123", "X"), "MM/YY"),
        (MockCard("4242 4242 4242 4242", "12/30", "12", "X"), "CVV"),
        (MockCard("4242 4242 4242 4242", "12/30", "123", " "), "name"),
    ],
)
async def test_invalid_card_details_are_refused(db, card, message):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    with pytest.raises(RemittanceError, match=message):
        await create_remittance(db, sender, beneficiary_id=ben.id, zar_amount=Decimal("100"), card=card)


# --- cash-in outcomes (FR-CI-02..04, FR-MQ-01) ---

async def test_received_queues_exactly_one_settlement(db):
    txn, _, _ = await remittance(db)
    enqueue = RecordingEnqueue()

    await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=enqueue)

    assert txn.cashin_status == CashInStatus.received
    assert txn.cashin_updated_at is not None
    assert txn.settlement_status == SettlementStatus.queued
    assert enqueue.calls == [(str(txn.idempotency_key), str(txn.id))]


async def test_double_confirm_does_not_publish_twice(db):
    txn, _, _ = await remittance(db)
    enqueue = RecordingEnqueue()
    await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=enqueue)

    with pytest.raises(RemittanceError, match="already been processed"):
        await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=enqueue)
    assert len(enqueue.calls) == 1


async def test_failed_cashin_halts_without_enqueue(db):
    txn, sender, _ = await remittance(db, "500")
    enqueue = RecordingEnqueue()

    await cashin_service.mark_cashin_failed(db, txn, reviewer=None, reason="Card reversed")

    assert txn.cashin_status == CashInStatus.failed
    assert txn.settlement_status == SettlementStatus.not_queued
    assert txn.status_label == "Failed"
    assert txn.failure_reason == "Card reversed"
    assert enqueue.calls == []
    # A failed cash-in moved no money, so it frees the allowance.
    assert await get_daily_usage(db, sender.id) == Decimal("0")

    with pytest.raises(RemittanceError):
        await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=enqueue)
    assert enqueue.calls == []


async def test_declined_test_card_fails_immediately(db):
    txn, _, _ = await remittance(db, card=DECLINED_CARD)
    assert txn.cashin_status == CashInStatus.failed
    assert "declined" in txn.failure_reason
    assert txn.settlement_status == SettlementStatus.not_queued


async def test_queue_outage_is_visible_and_retryable(db):
    txn, _, _ = await remittance(db)
    await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=RecordingEnqueue(fail=True))

    assert txn.settlement_status == SettlementStatus.failed
    assert txn.xrpl_error_reason.startswith("queue_unavailable")
    assert txn in await cashin_service.list_settlement_issues(db)

    enqueue = RecordingEnqueue()
    assert await cashin_service.retry_settlement(db, txn, enqueue=enqueue) == "requeued"
    assert txn.settlement_status == SettlementStatus.queued
    assert len(enqueue.calls) == 1


# --- routes ---

class FakeQueue:
    def __init__(self):
        self.jobs = []

    def enqueue(self, func, *args, **kwargs):
        self.jobs.append((func, args, kwargs))
        return type("Job", (), {"id": f"job-{len(self.jobs)}"})()


async def test_send_flow_pages_render(client, db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    with logged_in(sender):
        r = await client.get("/send")
        assert r.status_code == 200 and ben.full_name in r.text

        r = await client.get(f"/send/review?beneficiary_id={ben.id}&zar_amount=1000")
        assert r.status_code == 200
        assert "50.874404 UCTUSD" in r.text and "Continue to payment" in r.text

        r = await client.get(f"/send/pay?beneficiary_id={ben.id}&zar_amount=1000")
        assert r.status_code == 200 and "R1,000.00" in r.text


async def test_post_remittance_creates_transaction_and_redirects(client, db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    form = {
        "beneficiary_id": str(ben.id), "zar_amount": "1000", "exchange_rate": "18.870000",
        "card_number": "4242 4242 4242 4242", "card_expiry": "12/30", "card_cvv": "123", "card_name": "T",
    }
    with logged_in(sender):
        r = await client.post("/remittances", data=form, follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"].startswith("/transactions/")

        detail = await client.get(r.headers["location"])
        assert detail.status_code == 200 and "Awaiting payment" in detail.text

        history = await client.get("/transactions")
        assert ben.full_name in history.text


async def test_post_remittance_bad_card_rerenders_pay_page(client, db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    form = {
        "beneficiary_id": str(ben.id), "zar_amount": "1000", "exchange_rate": "",
        "card_number": "1234", "card_expiry": "12/30", "card_cvv": "123", "card_name": "T",
    }
    with logged_in(sender):
        r = await client.post("/remittances", data=form, follow_redirects=False)
    assert r.status_code == 200 and "Card number is not valid" in r.text
    assert await _count(db, sender.id) == 0


async def test_unapproved_sender_redirected_from_send(client, db):
    from app.services.auth_service import create_user
    user = await create_user(db, full_name="New User", email="newuser_ci@test.com",
                             mobile="+27795550002", password="Pass1234!")
    with logged_in(user):
        r = await client.get("/send", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/dashboard"


async def test_other_users_cannot_view_a_transaction(client, db):
    txn, _, _ = await remittance(db)
    stranger = await approved_sender(db)
    with logged_in(stranger):
        r = await client.get(f"/transactions/{txn.id}", follow_redirects=False)
    assert r.status_code == 302


async def test_admin_cashin_queue_confirm_publishes(client, db, monkeypatch):
    queue = FakeQueue()
    monkeypatch.setattr(queue_service, "get_queue", lambda: queue)
    txn, _, _ = await remittance(db)
    admin = await approved_sender(db, is_admin=True)

    with logged_in(admin):
        page = await client.get("/admin/cashin")
        assert page.status_code == 200 and "•••• 4242" in page.text
        r = await client.post(f"/admin/cashin/{txn.id}/received", follow_redirects=False)
    assert r.status_code == 302

    await db.refresh(txn)
    assert txn.cashin_status == CashInStatus.received
    assert txn.cashin_reviewed_by == admin.id
    func, args, kwargs = queue.jobs[0]
    assert func == queue_service.SETTLE_JOB
    assert args == (str(txn.idempotency_key),)
    assert kwargs["meta"] == {"transaction_id": str(txn.id)}  # FR-MQ-03 traceability


async def test_cashin_patch_api_is_admin_only(client, db, monkeypatch):
    monkeypatch.setattr(queue_service, "get_queue", lambda: FakeQueue())
    txn, sender, _ = await remittance(db)

    with logged_in(sender):
        r = await client.patch(f"/transactions/{txn.id}/cashin", json={"status": "received"})
    assert r.status_code == 403

    admin = await approved_sender(db, is_admin=True)
    with logged_in(admin):
        r = await client.patch(f"/transactions/{txn.id}/cashin", json={"status": "failed", "reason": "No funds"})
        assert r.status_code == 200
        assert r.json()["cashin_status"] == "failed" and r.json()["cashin_updated_at"]
        again = await client.patch(f"/transactions/{txn.id}/cashin", json={"status": "received"})
        assert again.status_code == 409
