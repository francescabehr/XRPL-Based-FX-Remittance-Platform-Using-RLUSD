"""FR-MQ-01..06, FR-WAL-05..07  Settlement queue + worker. XRPL and Redis are faked."""
import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.models.transaction import SettlementStatus, Transaction
from app.models.wallet import Wallet
from app.services import cashin_service, queue_service, xrpl_service
from app.workers import settlement_worker
from app.workers.settlement_worker import MAX_ATTEMPTS, process_settlement
from tests.conftest import _TestSession
from tests.remit_helpers import FakeXRPL, RecordingEnqueue, RecordingRequeue, remittance

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")


@pytest.fixture
def xrpl(monkeypatch):
    fake = FakeXRPL()
    monkeypatch.setattr(xrpl_service, "provision_wallet", fake.provision_wallet)
    monkeypatch.setattr(xrpl_service, "send_from_treasury", fake.send_from_treasury)
    return fake


async def _queued(db, amount="1000"):
    txn, sender, recipient = await remittance(db, amount)
    await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=RecordingEnqueue())
    return txn, recipient


async def _fresh(txn_id):
    async with _TestSession() as s:
        return await s.get(Transaction, txn_id)


async def _balance(user_id):
    async with _TestSession() as s:
        w = (await s.execute(select(Wallet).where(Wallet.user_id == user_id))).scalar_one_or_none()
        return None if w is None else w.balance_uctusd


def _settle(txn, requeue=None, lock=None):
    # no_lock by default: these tests have no Redis, and the treasury lock only
    # matters when several workers sign concurrently (see the #15 test below).
    return process_settlement(
        txn.idempotency_key, _TestSession,
        requeue=requeue or RecordingRequeue(),
        lock=lock or settlement_worker.no_lock,
    )


# --- publishing (FR-MQ-01, FR-MQ-03) ---

def test_enqueue_carries_idempotency_key_and_transaction_id(monkeypatch):
    class Q:
        def enqueue(self, func, *args, **kwargs):
            self.call = (func, args, kwargs)
            return type("Job", (), {"id": "j1"})()

    q = Q()
    monkeypatch.setattr(queue_service, "get_queue", lambda: q)

    assert queue_service.enqueue_settlement("key-1", "txn-1") == "j1"
    func, args, kwargs = q.call
    assert func == "app.workers.settlement_worker.settle"
    assert args == ("key-1",)
    assert kwargs["meta"] == {"transaction_id": "txn-1"}


# --- happy path (FR-MQ-02, FR-WAL-01, FR-WAL-05/06) ---

async def test_settlement_completes_and_credits_once(db, xrpl):
    txn, recipient = await _queued(db)

    assert await _settle(txn) == "completed"

    done = await _fresh(txn.id)
    assert done.settlement_status == SettlementStatus.completed
    assert done.xrpl_tx_hash and done.settled_at and done.xrpl_error_reason is None
    assert done.settlement_attempts == 1
    assert await _balance(recipient.id) == Decimal("50.874404")
    assert xrpl.provisioned == 1 and len(xrpl.payments) == 1


async def test_recipient_becomes_a_receiver(db, xrpl):
    txn, recipient = await _queued(db)
    await _settle(txn)
    await db.refresh(recipient)
    assert recipient.can_receive is True


# --- idempotency (FR-MQ-04) ---

async def test_redelivered_message_is_a_no_op(db, xrpl):
    txn, recipient = await _queued(db)

    assert await _settle(txn) == "completed"
    assert await _settle(txn) == "skipped"

    assert len(xrpl.payments) == 1
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_concurrent_deliveries_pay_once(db, xrpl):
    txn, recipient = await _queued(db)
    xrpl.delay = 0.05  # keep the first payment in flight while the second arrives

    results = await asyncio.gather(_settle(txn), _settle(txn), _settle(txn))

    assert sorted(results) == ["completed", "skipped", "skipped"]
    assert len(xrpl.payments) == 1
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_message_for_unconfirmed_cashin_is_ignored(db, xrpl):
    txn, _, _ = await remittance(db)  # cash-in still pending
    assert await _settle(txn) == "skipped"
    assert xrpl.payments == []
    assert (await _fresh(txn.id)).settlement_status == SettlementStatus.not_queued


async def test_complete_settlement_twice_credits_once(db, xrpl):
    txn, recipient = await _queued(db)
    await _settle(txn)
    async with _TestSession() as s:
        again = await s.get(Transaction, txn.id)
        # Already completed, so the conditional update matches nothing: no second credit.
        assert await settlement_worker.complete_settlement(s, again, again.xrpl_tx_hash) is False
    assert await _balance(recipient.id) == Decimal("50.874404")


# --- guarded transitions (AUDIT #6, #7, #8, #14) ---

async def test_fail_settlement_cannot_drag_back_a_requeued_row(db, xrpl):
    """AUDIT #6: fail_settlement was a plain ORM write with no WHERE guard, so a
    late worker could stamp `failed` over a row an admin had just re-queued —
    and overwrite its hash while doing it.

    Here the row is `queued` (a fresh attempt is pending) while a worker that has
    lost its claim tries to fail it from `processing`.
    """
    txn, _ = await _queued(db)

    async with _TestSession() as s:
        stale = await s.get(Transaction, txn.id)   # the losing worker's view
        assert await settlement_worker.fail_settlement(s, stale, "late failure", "LATEHASH") is False

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.queued  # untouched
    assert row.xrpl_tx_hash is None                          # hash not overwritten
    assert row.xrpl_error_reason is None


async def test_fail_settlement_from_an_unexpected_state_is_a_no_op(db, xrpl):
    txn, recipient = await _queued(db)
    assert await _settle(txn) == "completed"

    async with _TestSession() as s:
        done = await s.get(Transaction, txn.id)
        assert await settlement_worker.fail_settlement(s, done, "too late") is False

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.completed  # still completed
    assert row.xrpl_error_reason is None
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_publish_failure_cannot_clobber_a_live_worker(db, xrpl):
    """AUDIT #7: Redis can accept the job and still raise. The failure write then
    landed on a row a worker had already moved to `processing`, so the payment
    settled on-ledger while complete_settlement matched nothing and the recipient
    was never credited."""
    txn, sender, recipient = await remittance(db, "1000")
    await xrpl.provision_wallet(db, recipient)  # as the worker would, before paying

    # Redis took the job and a worker claimed it; only the response was lost. The
    # claim happens before the publish raises, which is the ordering that matters.
    await cashin_service.mark_cashin_received(db, txn, reviewer=None, enqueue=RecordingEnqueue())
    async with _TestSession() as s:
        assert await settlement_worker.claim(s, txn.idempotency_key) is not None  # worker owns it

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        await cashin_service._publish(s, row, RecordingEnqueue(fail=True))

    # The worker still owns the row, so it can finish and credit the recipient.
    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.processing  # NOT stamped failed
    async with _TestSession() as s:
        owned = await s.get(Transaction, txn.id)
        assert await settlement_worker.complete_settlement(s, owned, "HASHOK") is True
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_publish_failure_still_marks_a_row_nobody_claimed(db, xrpl):
    """The other side of #7: when no worker took it, the row must still fail
    visibly so an admin can retry it."""
    txn, _, _ = await remittance(db, "1000")
    await cashin_service.mark_cashin_received(
        db, txn, reviewer=None, enqueue=RecordingEnqueue(fail=True)
    )

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.failed
    assert row.xrpl_error_reason.startswith("queue_unavailable")


async def test_completing_without_a_wallet_row_does_not_strand_the_transaction(db, xrpl):
    """AUDIT #8: scalar_one() raised after the status update had already run, so
    the rollback left the row in `processing` after a validated payment."""
    from sqlalchemy import delete
    from app.models.wallet import Wallet

    txn, recipient = await _queued(db)
    async with _TestSession() as s:
        claimed = await settlement_worker.claim(s, txn.idempotency_key)
        await s.execute(delete(Wallet).where(Wallet.user_id == recipient.id))
        await s.commit()
        assert await settlement_worker.complete_settlement(s, claimed, "HASHNOWALLET") is True

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.completed  # not stuck in processing
    assert row.xrpl_tx_hash == "HASHNOWALLET"


async def test_a_row_that_was_signed_can_never_be_claimed_again(db, xrpl):
    """AUDIT #14: the guard the burn worker already had. No queued row should
    carry a hash, so this is defence in depth against a double submission."""
    txn, _ = await _queued(db)

    async with _TestSession() as s:
        await s.execute(
            update(Transaction).where(Transaction.id == txn.id).values(xrpl_tx_hash="ALREADYSIGNED")
        )
        await s.commit()

    async with _TestSession() as s:
        assert await settlement_worker.claim(s, txn.idempotency_key) is None

    assert xrpl.payments == []
    assert (await _fresh(txn.id)).settlement_status == SettlementStatus.queued


async def test_the_payment_is_signed_inside_the_treasury_lock(db, xrpl, monkeypatch):
    """AUDIT #15: every settlement is signed by the one treasury account, and
    xrpl-py reads that account's next sequence number during autofill. Two
    concurrent signings take the SAME sequence and the loser is rejected
    tefPAST_SEQ, so signing must happen while the lock is held.

    Concurrency here is across worker processes (RQ runs one job at a time per
    worker), which is what the Redis lock serialises; this asserts the lock
    brackets the payment, which is the part the worker controls.
    """
    from contextlib import contextmanager

    events = []

    @contextmanager
    def recording_lock():
        events.append("acquired")
        try:
            yield
        finally:
            events.append("released")

    original = xrpl.send_from_treasury

    async def watched(*args, **kwargs):
        events.append("signed")
        return await original(*args, **kwargs)

    monkeypatch.setattr(xrpl_service, "send_from_treasury", watched)

    txn, _ = await _queued(db)
    assert await _settle(txn, lock=recording_lock) == "completed"

    # Signed strictly between acquire and release — never outside it.
    assert events == ["acquired", "signed", "released"]


def test_treasury_lock_is_one_blocking_named_lock():
    """One lock for one signing account: a per-transaction lock would serialise
    nothing, because the sequence belongs to the treasury, not the payment."""
    assert queue_service.TREASURY_LOCK == "settlement:treasury-sequence"
    # Must outlive an XRPL round trip, but expire if a worker is killed holding it.
    assert queue_service.TREASURY_LOCK_TIMEOUT >= queue_service.JOB_TIMEOUT
    # Must wait its turn rather than give up immediately.
    assert queue_service.TREASURY_LOCK_WAIT > 0


# --- ledger failures (FR-WAL-07, FR-MQ-05) ---

@pytest.mark.parametrize("code", ["tecPATH_DRY", "tecPATH_PARTIAL"])
async def test_ledger_failure_is_recorded_and_not_retried(db, xrpl, code):
    txn, recipient = await _queued(db)
    xrpl.outcomes.append(code)
    requeue = RecordingRequeue()

    assert await _settle(txn, requeue) == "failed"

    failed = await _fresh(txn.id)
    assert failed.settlement_status == SettlementStatus.failed
    assert failed.xrpl_error_reason.startswith(code)
    assert failed.xrpl_tx_hash  # traceable on the ledger
    assert failed.status_label == "Failed"
    assert await _balance(recipient.id) == Decimal("0")
    assert requeue.calls == []  # post-signing failures are never auto-retried


async def test_failure_after_signing_is_outcome_unknown(db, xrpl):
    txn, recipient = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("network dropped")))
    requeue = RecordingRequeue()

    assert await _settle(txn, requeue) == "failed"

    failed = await _fresh(txn.id)
    assert failed.xrpl_error_reason.startswith("outcome_unknown")
    assert failed.xrpl_tx_hash
    assert requeue.calls == []
    assert await _balance(recipient.id) == Decimal("0")


# --- transient failures + retry policy (FR-MQ-06) ---

async def test_pre_signing_failure_is_requeued_with_backoff(db, xrpl):
    txn, _ = await _queued(db)
    xrpl.outcomes.append(ConnectionError("testnet unreachable"))
    requeue = RecordingRequeue()

    assert await _settle(txn, requeue) == "requeued"

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.queued
    assert row.xrpl_error_reason.startswith("retrying")
    assert requeue.calls == [(10, str(txn.idempotency_key), str(txn.id))]

    # The retry then succeeds normally.
    assert await _settle(txn, requeue) == "completed"
    assert (await _fresh(txn.id)).settlement_attempts == 2


async def test_untrusted_wallet_and_sign_errors_are_transient(db, xrpl):
    txn, _ = await _queued(db)
    xrpl.trust_ok = False
    assert await _settle(txn) == "requeued"

    xrpl.trust_ok = True
    xrpl.outcomes.append("sign_error")
    assert await _settle(txn) == "requeued"
    assert xrpl.payments == []


async def test_an_unusable_amount_fails_at_once_without_retrying(db, xrpl):
    """AUDIT #2: a zero/negative UCTUSD amount raised ValueError out of uctusd(),
    which was read as transient and burned three attempts before failing."""
    txn, _ = await _queued(db)
    xrpl.outcomes.append(xrpl_service.InvalidAmountError("UCTUSD amount must be positive."))
    requeue = RecordingRequeue()

    assert await _settle(txn, requeue) == "failed"

    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.failed
    assert row.xrpl_error_reason.startswith("invalid_amount")
    assert requeue.calls == []            # no pointless retries
    assert row.settlement_attempts == 1


async def test_retries_stop_after_max_attempts(db, xrpl):
    txn, _ = await _queued(db)
    xrpl.outcomes.extend([ConnectionError("down")] * MAX_ATTEMPTS)
    requeue = RecordingRequeue()

    results = [await _settle(txn, requeue) for _ in range(MAX_ATTEMPTS)]

    assert results == ["requeued"] * (MAX_ATTEMPTS - 1) + ["failed"]
    row = await _fresh(txn.id)
    assert row.settlement_status == SettlementStatus.failed
    assert row.xrpl_error_reason.startswith("retries_exhausted")
    assert txn.id in [t.id for t in await cashin_service.list_settlement_issues(db)]


# --- admin retry (FR-MQ-06, FR-ADM-05) ---

async def _age(txn_id, minutes):
    async with _TestSession() as s:
        await s.execute(
            update(Transaction).where(Transaction.id == txn_id)
            .values(updated_at=datetime.now(timezone.utc) - timedelta(minutes=minutes))
        )
        await s.commit()


async def test_admin_retry_reconciles_a_payment_that_actually_landed(db, xrpl):
    txn, recipient = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    async def ledger_says_success(tx_hash):
        return "tesSUCCESS"

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        enqueue = RecordingEnqueue()
        assert await cashin_service.retry_settlement(s, row, enqueue=enqueue, ledger_result=ledger_says_success) == "reconciled"
        assert enqueue.calls == []  # nothing re-sent

    done = await _fresh(txn.id)
    assert done.settlement_status == SettlementStatus.completed
    assert await _balance(recipient.id) == Decimal("50.874404")
    assert xrpl.payments == []  # the "lost" payment was never re-submitted


async def not_found(tx_hash):
    return None


def _ledger_at(index):
    async def latest():
        return index
    return latest


def _history(covered: bool):
    async def has_range(start, end):
        return covered
    return has_range


async def test_admin_retry_waits_while_a_signed_payment_could_still_land(db, xrpl):
    """AUDIT #3: re-sending now needs ledger proof that the attempt is dead.

    This test previously asserted the opposite — that waiting out
    LEDGER_EXPIRY_MARGIN (5 minutes of wall-clock) was enough to re-send. It is
    not: get_transaction_result returns None both for a payment that was never
    included and for one this node cannot see, so a validated payment whose
    response was lost would have been paid a second time. Aging the row no longer
    unlocks the resend; passing the LastLedgerSequence, with history to prove it,
    is what does.
    """
    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    # Still inside the range in which it could be validated.
    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        with pytest.raises(cashin_service.RemittanceError, match="could still be validated"):
            await cashin_service.retry_settlement(
                s, row, enqueue=RecordingEnqueue(), ledger_result=not_found,
                latest_ledger=_ledger_at(xrpl.last_ledger_sequence), ledger_range=_history(True),
            )

    # Time alone changes nothing — the whole point of the fix.
    await _age(txn.id, minutes=60)
    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        with pytest.raises(cashin_service.RemittanceError, match="could still be validated"):
            await cashin_service.retry_settlement(
                s, row, enqueue=RecordingEnqueue(), ledger_result=not_found,
                latest_ledger=_ledger_at(xrpl.last_ledger_sequence), ledger_range=_history(True),
            )

    # Past LastLedgerSequence, with unbroken history: now it is provably dead.
    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        enqueue = RecordingEnqueue()
        assert await cashin_service.retry_settlement(
            s, row, enqueue=enqueue, ledger_result=not_found,
            latest_ledger=_ledger_at(xrpl.last_ledger_sequence + 1), ledger_range=_history(True),
        ) == "requeued"
        assert len(enqueue.calls) == 1

    assert await _settle(txn) == "completed"


async def test_retry_archives_the_prior_attempt_hash_and_range(db, xrpl):
    """AUDIT #13: the retry cleared xrpl_tx_hash, destroying the only record of a
    payment that may exist on-ledger — the exact thing needed to investigate it.

    The attempt is archived hash AND range together: a hash with no range cannot
    be reasoned about, because "absent from the ledger" is only meaningful
    against the ledgers it could have appeared in (AUDIT #3).
    """
    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    before = await _fresh(txn.id)
    dead_hash = before.xrpl_tx_hash
    assert dead_hash and before.settlement_previous_attempts == []

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        assert await cashin_service.retry_settlement(
            s, row, enqueue=RecordingEnqueue(), ledger_result=not_found,
            latest_ledger=_ledger_at(xrpl.last_ledger_sequence + 1), ledger_range=_history(True),
        ) == "requeued"

    row = await _fresh(txn.id)
    # Cleared, so the row can be claimed and signed again (and so the #14 guard
    # on claim still lets it through).
    assert row.xrpl_tx_hash is None
    assert row.settlement_last_ledger_sequence is None
    assert row.settlement_submitted_ledger_index is None

    # ...but preserved, as a complete, investigable record.
    assert len(row.settlement_previous_attempts) == 1
    archived = row.settlement_previous_attempts[0]
    assert archived["tx_hash"] == dead_hash
    assert archived["last_ledger_sequence"] == xrpl.last_ledger_sequence
    assert archived["submitted_ledger_index"] == xrpl.submitted_ledger_index
    assert archived["retired_at"]

    # A second round trip appends rather than replaces.
    assert await _settle(txn) == "completed"
    assert len((await _fresh(txn.id)).settlement_previous_attempts) == 1


async def test_retry_of_an_unsigned_attempt_archives_nothing(db, xrpl):
    """Nothing was ever signed, so there is no attempt worth keeping."""
    txn, _ = await _queued(db)
    xrpl.outcomes.extend([ConnectionError("down")] * (MAX_ATTEMPTS + 1))
    for _ in range(MAX_ATTEMPTS):
        await _settle(txn, RecordingRequeue())

    failed = await _fresh(txn.id)
    assert failed.settlement_status == SettlementStatus.failed
    assert failed.xrpl_tx_hash is None

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        assert await cashin_service.retry_settlement(s, row, enqueue=RecordingEnqueue()) == "requeued"

    assert (await _fresh(txn.id)).settlement_previous_attempts == []


async def test_the_archive_survives_into_the_admin_drill_down(db, xrpl, client):
    """The record only earns its keep if an admin can see it."""
    from tests.remit_helpers import logged_in
    from tests.test_admin import admin_user

    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)
    dead_hash = (await _fresh(txn.id)).xrpl_tx_hash

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        await cashin_service.retry_settlement(
            s, row, enqueue=RecordingEnqueue(), ledger_result=not_found,
            latest_ledger=_ledger_at(xrpl.last_ledger_sequence + 1), ledger_range=_history(True),
        )

    admin = await admin_user(db)
    with logged_in(admin):
        page = await client.get(f"/admin/transactions/{txn.id}")

    assert page.status_code == 200
    assert "Earlier attempts" in page.text
    assert dead_hash in page.text


async def test_admin_retry_refuses_while_the_server_has_a_history_gap(db, xrpl):
    """AUDIT #3: past LastLedgerSequence, but the node cannot see the whole range,
    so "not found" proves nothing and re-sending could pay twice."""
    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        enqueue = RecordingEnqueue()
        with pytest.raises(cashin_service.RemittanceError, match="missing ledger history"):
            await cashin_service.retry_settlement(
                s, row, enqueue=enqueue, ledger_result=not_found,
                latest_ledger=_ledger_at(xrpl.last_ledger_sequence + 100),
                ledger_range=_history(False),
            )
        assert enqueue.calls == []

    assert (await _fresh(txn.id)).xrpl_tx_hash is not None  # the attempt is not erased


async def test_admin_retry_refuses_an_attempt_with_no_recorded_range(db, xrpl):
    """A row signed before migration 0009 can never be proven dead, so it is
    refused rather than re-sent on a guess."""
    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    async with _TestSession() as s:
        await s.execute(
            update(Transaction).where(Transaction.id == txn.id).values(
                settlement_last_ledger_sequence=None, settlement_submitted_ledger_index=None
            )
        )
        await s.commit()

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        with pytest.raises(cashin_service.RemittanceError, match="no recorded ledger range"):
            await cashin_service.retry_settlement(
                s, row, enqueue=RecordingEnqueue(), ledger_result=not_found,
                latest_ledger=_ledger_at(10**9), ledger_range=_history(True),
            )


async def test_the_settlement_ledger_range_is_persisted_with_the_hash(db, xrpl):
    """The range must be written before submission, or nothing above can work."""
    txn, _ = await _queued(db)
    xrpl.outcomes.append(("raise_after_sign", TimeoutError("lost response")))
    await _settle(txn)

    row = await _fresh(txn.id)
    assert row.xrpl_tx_hash is not None
    assert row.settlement_last_ledger_sequence == xrpl.last_ledger_sequence
    assert row.settlement_submitted_ledger_index == xrpl.submitted_ledger_index


async def test_admin_retry_of_ledger_failure_resends(db, xrpl):
    txn, recipient = await _queued(db)
    xrpl.outcomes.append("tecPATH_DRY")
    await _settle(txn)

    async def ledger_says_failed(tx_hash):
        return "tecPATH_DRY"

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        assert await cashin_service.retry_settlement(
            s, row, enqueue=RecordingEnqueue(), ledger_result=ledger_says_failed
        ) == "requeued"

    assert await _settle(txn) == "completed"
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_only_failed_settlements_can_be_retried(db, xrpl):
    txn, _ = await _queued(db)
    await _settle(txn)
    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        with pytest.raises(cashin_service.RemittanceError):
            await cashin_service.retry_settlement(s, row, enqueue=RecordingEnqueue())


async def _claim_then_die(txn, xrpl=None, tx_hash=None):
    """Simulate a worker that claimed the message and then crashed.

    With tx_hash, it crashed after provisioning the wallet and signing the payment.
    """
    async with _TestSession() as s:
        row = await settlement_worker.claim(s, txn.idempotency_key)
        if tx_hash:
            from app.models.user import User
            await xrpl.provision_wallet(s, await s.get(User, row.recipient_user_id))
            row.xrpl_tx_hash = tx_hash
            await s.commit()


async def test_stuck_processing_cannot_be_recovered_while_a_worker_may_own_it(db, xrpl):
    txn, _ = await _queued(db)
    await _claim_then_die(txn)

    assert txn.id not in [t.id for t in await cashin_service.list_settlement_issues(db)]
    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        with pytest.raises(cashin_service.RemittanceError, match="failed or stuck"):
            await cashin_service.retry_settlement(s, row, enqueue=RecordingEnqueue())


async def test_worker_died_before_signing_is_requeued_and_settles(db, xrpl):
    txn, recipient = await _queued(db)
    await _claim_then_die(txn)  # the macOS fork crash seen in the live run
    await _age(txn.id, minutes=11)
    assert txn.id in [t.id for t in await cashin_service.list_settlement_issues(db)]

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        enqueue = RecordingEnqueue()
        assert await cashin_service.retry_settlement(s, row, enqueue=enqueue) == "requeued"
        assert len(enqueue.calls) == 1

    assert await _settle(txn) == "completed"
    assert await _balance(recipient.id) == Decimal("50.874404")


async def test_worker_died_after_signing_checks_ledger_first(db, xrpl):
    txn, recipient = await _queued(db)
    await _claim_then_die(txn, xrpl, tx_hash="SIGNEDHASH")
    await _age(txn.id, minutes=11)

    async def ledger_says_success(tx_hash):
        assert tx_hash == "SIGNEDHASH"
        return "tesSUCCESS"

    async with _TestSession() as s:
        row = await s.get(Transaction, txn.id)
        enqueue = RecordingEnqueue()
        assert await cashin_service.retry_settlement(
            s, row, enqueue=enqueue, ledger_result=ledger_says_success
        ) == "reconciled"
        assert enqueue.calls == []

    assert (await _fresh(txn.id)).settlement_status == SettlementStatus.completed
    assert await _balance(recipient.id) == Decimal("50.874404")


# --- recipient wallet screen (FR-WAL-05) ---

async def test_wallet_page_lists_settled_transfer_with_hash(client, db, xrpl, monkeypatch):
    from tests.remit_helpers import logged_in

    async def no_ledger(address, client=None):
        raise ConnectionError("offline")

    monkeypatch.setattr(xrpl_service, "get_uctusd_balance", no_ledger)
    txn, recipient = await _queued(db)
    await _settle(txn)
    done = await _fresh(txn.id)
    # The worker wrote through its own session; drop this session's cached copies
    # (in the app, every request gets a fresh session).
    db.expire_all()
    await db.refresh(recipient)

    with logged_in(recipient):
        r = await client.get("/wallet")
    assert r.status_code == 200
    assert "50.874404" in r.text
    assert f"https://testnet.xrpl.org/transactions/{done.xrpl_tx_hash}" in r.text
    assert "Ledger balance unavailable" in r.text
