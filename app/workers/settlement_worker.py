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

Retry policy (FR-MQ-06):
  - Failure before signing (network, faucet, TrustSet not yet set): nothing reached
    the ledger, so re-queue with a delay, up to MAX_ATTEMPTS, then mark failed.
  - Failure after signing: never retried automatically — the payment may exist.
    It is marked failed and shown to admins, whose retry checks the ledger first.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

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
    """
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.idempotency_key == idempotency_key,
            Transaction.settlement_status == SettlementStatus.queued,
            Transaction.cashin_status == CashInStatus.received,
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
    ).scalar_one()
    wallet.balance_uctusd = Decimal(wallet.balance_uctusd) + Decimal(txn.uctusd_amount)
    await db.commit()
    await db.refresh(txn)
    logger.info("Settled transaction %s: %s", txn.id, tx_hash)
    return True


async def fail_settlement(
    db: AsyncSession, txn: Transaction, reason: str, tx_hash: Optional[str] = None
) -> None:
    txn.settlement_status = SettlementStatus.failed
    txn.xrpl_error_reason = reason
    if tx_hash:
        txn.xrpl_tx_hash = tx_hash
    db.add(txn)
    await db.commit()
    logger.warning("Settlement failed for transaction %s: %s", txn.id, reason)


async def _retry_or_fail(
    db: AsyncSession, txn: Transaction, reason: str, requeue: Requeue
) -> str:
    if txn.settlement_attempts >= MAX_ATTEMPTS:
        await fail_settlement(db, txn, f"retries_exhausted: {reason}")
        return "failed"

    delay = RETRY_DELAYS[min(txn.settlement_attempts - 1, len(RETRY_DELAYS) - 1)]
    txn.settlement_status = SettlementStatus.queued
    txn.xrpl_error_reason = f"retrying: {reason}"
    db.add(txn)
    await db.commit()
    requeue(delay, str(txn.idempotency_key), str(txn.id))
    logger.info("Re-queued transaction %s in %ss (attempt %s)", txn.id, delay, txn.settlement_attempts)
    return "requeued"


async def process_settlement(
    idempotency_key: uuid.UUID,
    session_factory: async_sessionmaker,
    requeue: Requeue = queue_service.enqueue_settlement_in,
) -> str:
    """Settle one message. Returns skipped | completed | failed | requeued."""
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

        async def remember_hash(tx_hash: str) -> None:
            nonlocal signed_hash
            signed_hash = tx_hash
            txn.xrpl_tx_hash = tx_hash
            db.add(txn)
            await db.commit()

        try:
            wallet = await xrpl_service.provision_wallet(db, recipient)
            if not wallet.trust_set_complete:
                raise TransientSettlementError("trust line to the issuer is not set yet")
            result = await xrpl_service.send_from_treasury(
                wallet.xrpl_address, Decimal(txn.uctusd_amount), on_signed=remember_hash
            )
        except Exception as exc:  # noqa: BLE001 — every path must leave the row in a known state
            if signed_hash:
                await fail_settlement(
                    db, txn,
                    f"outcome_unknown: {type(exc).__name__} after signing; check the hash on the ledger",
                    signed_hash,
                )
                return "failed"
            return await _retry_or_fail(db, txn, f"{type(exc).__name__}: {exc}", requeue)

        if result.success:
            await complete_settlement(db, txn, result.tx_hash)
            return "completed"
        if result.result_code == "sign_error":
            return await _retry_or_fail(db, txn, f"sign_error: {result.message}", requeue)

        await fail_settlement(db, txn, f"{result.result_code}: {result.message}".strip(": "), result.tx_hash)
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
