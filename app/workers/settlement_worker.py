"""
FR-MQ-02..06, FR-WAL-01..07  RQ job: settle one transaction on XRPL.

Flow for one message:
  1. Claim the idempotency_key: one conditional UPDATE queued -> processing.
     A redelivered or duplicate message matches no row and stops here (FR-MQ-04).
  2. Make sure the recipient has a trust-lined wallet (provision if not).
  3. Treasury -> recipient UCTUSD payment. The hash is saved before submission.
  4. tesSUCCESS -> completed + wallet balance credited, in one DB transaction,
     only after the ledger validated the payment (FR-WAL-06).
     tec* etc. -> failed with the ledger's reason; balance untouched (FR-WAL-07).

Concurrency: settlements may run on several workers, but the treasury signing
step is serialised by a Redis lock (queue_service.treasury_lock) because every
payment is signed by the one treasury account and its sequence number is read at
autofill time. Without that, two workers take the same sequence and one payment
is rejected tefPAST_SEQ.

Retry policy (FR-MQ-06):
  - Failure before signing (network, faucet, TrustSet not yet set): nothing reached
    the ledger, so re-queue with a delay, up to MAX_ATTEMPTS, then mark failed.
  - A permanently unusable amount: failed immediately — retrying cannot change it.
  - Failure after signing: never retried automatically — the payment may exist.
    It is marked failed and shown to admins, whose retry checks the ledger first.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, ContextManager, Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models.transaction import CashInStatus, SettlementStatus, Transaction
from app.models.user import User
from app.models.wallet import Wallet
from app.services import queue_service, xrpl_service

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS = (10, 30, 60)  # seconds before attempt 2, 3, ...

Requeue = Callable[[int, str, str], object]
# Returns a context manager serialising treasury signing. Late-bound like the
# queue publishers (see CLAUDE.md): a default argument would bind the real Redis
# lock at import time and silently defeat substitution in tests.
TreasuryLock = Callable[[], ContextManager]


@contextmanager
def no_lock():
    """For callers with a single signer, or tests: serialises nothing."""
    yield


class TransientSettlementError(RuntimeError):
    """Failed before anything was signed — safe to retry."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reload(db: AsyncSession, txn_id: uuid.UUID) -> Transaction:
    result = await db.execute(
        select(Transaction)
        .where(Transaction.id == txn_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def claim(db: AsyncSession, idempotency_key: uuid.UUID) -> Optional[Transaction]:
    """Atomically take ownership of a queued settlement (FR-MQ-04).

    Only one caller can move a row from queued to processing, so concurrent or
    repeated deliveries of the same message cannot both proceed.

    `xrpl_tx_hash IS NULL` is the same defence the burn worker relies on: a row
    that has been signed for must never be picked up and signed for again. No
    queued row should ever carry a hash (a retry archives the old attempt and
    clears the field), so this changes nothing today — it just makes a double
    submission structurally impossible rather than merely unreachable.
    """
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.idempotency_key == idempotency_key,
            Transaction.settlement_status == SettlementStatus.queued,
            Transaction.cashin_status == CashInStatus.received,
            Transaction.xrpl_tx_hash.is_(None),
        )
        .values(
            settlement_status=SettlementStatus.processing,
            settlement_attempts=Transaction.settlement_attempts + 1,
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    txn_id = result.scalar_one_or_none()
    await db.commit()
    return None if txn_id is None else await _reload(db, txn_id)


async def complete_settlement(
    db: AsyncSession,
    txn: Transaction,
    tx_hash: str,
    from_statuses: Iterable[SettlementStatus] = (SettlementStatus.processing,),
) -> bool:
    """Mark completed and credit the recipient's wallet cache — once.

    The status change and the credit commit together; the conditional update makes
    a second call a no-op, and the wallet row is locked against concurrent credits.
    A missing wallet row is handled rather than raised: the payment has validated,
    so the transition must not be rolled back by a cache problem.
    """
    result = await db.execute(
        update(Transaction)
        .where(Transaction.id == txn.id, Transaction.settlement_status.in_(list(from_statuses)))
        .values(
            settlement_status=SettlementStatus.completed,
            xrpl_tx_hash=tx_hash,
            xrpl_error_reason=None,
            settled_at=_now(),
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # already settled — nothing changed
        return False

    wallet = (
        await db.execute(
            select(Wallet).where(Wallet.user_id == txn.recipient_user_id).with_for_update()
        )
    ).scalar_one_or_none()
    if wallet is None:
        # scalar_one() used to raise here, after the status update had already run:
        # the exception rolled the whole transaction back and left the row in
        # `processing` even though the payment had validated on-ledger.
        # The ledger is authoritative for balances and the payment did land, so
        # completing is correct; only the cache has no row to update. Loud, because
        # a settled recipient with no wallet row is a real inconsistency.
        logger.error(
            "Settled transaction %s (%s) but recipient %s has no wallet row to credit",
            txn.id, tx_hash, txn.recipient_user_id,
        )
    else:
        wallet.balance_uctusd = Decimal(wallet.balance_uctusd) + Decimal(txn.uctusd_amount)
    await db.commit()
    await db.refresh(txn)
    logger.info("Settled transaction %s: %s", txn.id, tx_hash)
    return True


async def fail_settlement(
    db: AsyncSession,
    txn: Transaction,
    reason: str,
    tx_hash: Optional[str] = None,
    from_statuses: Iterable[SettlementStatus] = (SettlementStatus.processing,),
) -> bool:
    """Mark a settlement failed — once, and only from an expected state.

    Conditional like every sibling transition: a worker that lost the row (its
    claim expired, an admin re-queued it, another attempt completed it) must not
    be able to drag it back to failed and overwrite the hash of an attempt it no
    longer owns.
    """
    values = {
        "settlement_status": SettlementStatus.failed,
        "xrpl_error_reason": reason,
        "updated_at": _now(),
    }
    if tx_hash:
        values["xrpl_tx_hash"] = tx_hash

    result = await db.execute(
        update(Transaction)
        .where(Transaction.id == txn.id, Transaction.settlement_status.in_(list(from_statuses)))
        .values(**values)
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # someone else owns this row now — leave it alone
        logger.warning(
            "Settlement %s not failed (%s): no longer in %s",
            txn.id, reason, [s.value for s in from_statuses],
        )
        return False
    await db.commit()
    await db.refresh(txn)
    logger.warning("Settlement failed for transaction %s: %s", txn.id, reason)
    return True


async def _retry_or_fail(
    db: AsyncSession, txn: Transaction, reason: str, requeue: Requeue
) -> str:
    if txn.settlement_attempts >= MAX_ATTEMPTS:
        await fail_settlement(db, txn, f"retries_exhausted: {reason}")
        return "failed"

    delay = RETRY_DELAYS[min(max(txn.settlement_attempts, 1) - 1, len(RETRY_DELAYS) - 1)]
    # Conditional for the same reason fail_settlement is: only the worker that
    # still holds the claim may hand the row back to the queue.
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.id == txn.id,
            Transaction.settlement_status == SettlementStatus.processing,
        )
        .values(
            settlement_status=SettlementStatus.queued,
            xrpl_error_reason=f"retrying: {reason}",
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()
        logger.info("Settlement %s not re-queued: no longer processing", txn.id)
        return "skipped"
    await db.commit()
    await db.refresh(txn)
    requeue(delay, str(txn.idempotency_key), str(txn.id))
    logger.info("Re-queued transaction %s in %ss (attempt %s)", txn.id, delay, txn.settlement_attempts)
    return "requeued"


async def process_settlement(
    idempotency_key: uuid.UUID,
    session_factory: async_sessionmaker,
    requeue: Requeue = queue_service.enqueue_settlement_in,
    lock: Optional[TreasuryLock] = None,
) -> str:
    """Settle one message. Returns skipped | completed | failed | requeued."""
    # Late-bound, per CLAUDE.md: resolved here, not in the signature.
    lock = lock or queue_service.treasury_lock
    async with session_factory() as db:
        txn = await claim(db, idempotency_key)
        if txn is None:
            logger.info("Settlement %s not claimable (duplicate or not queued) — skipped", idempotency_key)
            return "skipped"

        recipient = await db.get(User, txn.recipient_user_id) if txn.recipient_user_id else None
        if recipient is None:
            await fail_settlement(db, txn, "recipient_not_registered: no platform user to receive funds")
            return "failed"

        signed_hash: Optional[str] = None

        async def remember_signed(signed: xrpl_service.SignedTx) -> None:
            """Persist hash + ledger range before the payment is submitted.

            The range is what an admin retry needs to prove a missing payment can
            never be included. If this raises, nothing is submitted, so the row
            (still without a hash) stays safe to retry.
            """
            nonlocal signed_hash
            signed_hash = signed.tx_hash
            txn.xrpl_tx_hash = signed.tx_hash
            txn.settlement_last_ledger_sequence = signed.last_ledger_sequence
            txn.settlement_submitted_ledger_index = signed.submitted_ledger_index
            db.add(txn)
            await db.commit()

        try:
            wallet = await xrpl_service.provision_wallet(db, recipient)
            if not wallet.trust_set_complete:
                raise TransientSettlementError("trust line to the issuer is not set yet")
            # Serialised across workers: the treasury's next sequence number is
            # read during autofill, so two concurrent signings would take the
            # same one and the loser would be rejected tefPAST_SEQ.
            with lock():
                result = await xrpl_service.send_from_treasury(
                    wallet.xrpl_address, Decimal(txn.uctusd_amount),
                    on_signed_tx=remember_signed,
                )
        except xrpl_service.InvalidAmountError as exc:
            # The amount itself is unusable (a fee-consumed send booked before the
            # quote engine had a floor). Nothing was signed, and a retry re-reads
            # the same amount, so fail now instead of burning three attempts.
            await fail_settlement(db, txn, f"invalid_amount: {exc}")
            return "failed"
        except Exception as exc:  # noqa: BLE001 — every path must leave the row in a known state
            if signed_hash:
                await fail_settlement(
                    db, txn,
                    f"outcome_unknown: {type(exc).__name__} after signing; check the hash on the ledger",
                    signed_hash,
                )
                return "failed"
            return await _retry_or_fail(db, txn, f"{type(exc).__name__}: {exc}", requeue)

        # Branch on the resolution, never on the result code: a code scraped from a
        # timeout message can be the payment's *preliminary* result, not its outcome.
        if result.resolution == xrpl_service.SUCCEEDED:
            await complete_settlement(db, txn, result.tx_hash)
            return "completed"
        if result.resolution == xrpl_service.NOT_SUBMITTED:
            return await _retry_or_fail(
                db, txn, f"{result.result_code}: {result.message}".strip(": "), requeue
            )

        reason = f"{result.result_code}: {result.message}".strip(": ")
        if result.resolution == xrpl_service.UNKNOWN:
            # Signed and sent, outcome never seen. Marked with the same prefix as
            # the post-signing exception path so an admin retry knows it must
            # check the ledger before re-sending anything.
            reason = f"outcome_unknown: {reason}; check the hash on the ledger"
        await fail_settlement(db, txn, reason, result.tx_hash)
        return "failed"


async def _run(idempotency_key: uuid.UUID) -> str:
    # A fresh engine per job: RQ runs each job in its own event loop, and asyncpg
    # connections cannot be shared across loops.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        return await process_settlement(idempotency_key, factory)
    finally:
        await engine.dispose()


def settle(idempotency_key: str) -> str:
    """RQ entrypoint (sync). Enqueued by queue_service.enqueue_settlement."""
    return asyncio.run(_run(uuid.UUID(idempotency_key)))
