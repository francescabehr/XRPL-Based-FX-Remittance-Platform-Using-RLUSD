"""
FR-CO-03, FR-CO-05, FR-CO-06  RQ job: burn one approved cash-out on XRPL.

The settlement path in reverse. Where settlement pays treasury -> recipient and
credits on success, this pays recipient -> issuer (destroying the tokens) against
a balance that was already debited when the admin approved.

Flow for one message:
  1. Claim the row: one conditional UPDATE stamping burn_started_at. A redelivery,
     a duplicate message, or a row that anything has already signed for matches no
     row and stops here.
  2. Sign the burn, persist hash + ledger range BEFORE submitting.
  3. Three post-signing outcomes, and only these:
       validated tesSUCCESS -> completed, simulated fiat payout written, balance
                               untouched (it was debited at approval).
       validated tec*       -> failed, reserved UCTUSD restored exactly once.
       unknown              -> stays approved with failure_reason=outcome_unknown,
                               balance stays debited, never auto-resubmitted.
                               Only an admin reconcile resolves it, from the ledger.

Retry policy: only failures that provably never reached the ledger (signing, or an
error before signing) are retried, and the claim is released first. Nothing that
has been signed is ever resubmitted — that is what makes a double burn impossible.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models.cashout import CashOutRequest, CashOutStatus
from app.models.wallet import Wallet
from app.services import queue_service, xrpl_service

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_DELAYS = (10, 30, 60)  # seconds before attempt 2, 3, ...

# A claim older than this belonged to a worker that died. Only ever used to decide
# whether a worker is still alive — never as evidence about a ledger outcome — and
# only for rows that never got as far as signing (xrpl_burn_tx_hash IS NULL).
STUCK_AFTER = timedelta(minutes=10)  # > queue_service.JOB_TIMEOUT

UNKNOWN_PREFIX = "outcome_unknown"

Requeue = Callable[[int, str, str], object]


class TransientBurnError(RuntimeError):
    """Failed before anything was signed — safe to retry."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _payout_reference(req: CashOutRequest) -> str:
    """Reference for the simulated fiat payout. No real money moves anywhere."""
    return f"SIMPAY-{req.id.hex[:12].upper()}"


async def _reload(db: AsyncSession, req_id: uuid.UUID) -> CashOutRequest:
    result = await db.execute(
        select(CashOutRequest)
        .where(CashOutRequest.id == req_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def claim(db: AsyncSession, idempotency_key: uuid.UUID) -> Optional[CashOutRequest]:
    """Atomically take ownership of an approved cash-out.

    Three guards, each load-bearing:
      status == approved          nothing unapproved or already terminal is burned
      xrpl_burn_tx_hash IS NULL   once anything has been signed, NO worker may ever
                                  claim this row again — only reconcile resolves it
      burn_started_at unset/stale a live worker keeps its claim; a dead one's claim
                                  expires, but only because nothing was signed
    """
    stale_before = _now() - STUCK_AFTER
    result = await db.execute(
        update(CashOutRequest)
        .where(
            CashOutRequest.idempotency_key == idempotency_key,
            CashOutRequest.status == CashOutStatus.approved,
            CashOutRequest.xrpl_burn_tx_hash.is_(None),
            (CashOutRequest.burn_started_at.is_(None))
            | (CashOutRequest.burn_started_at < stale_before),
        )
        .values(
            burn_started_at=_now(),
            burn_attempts=CashOutRequest.burn_attempts + 1,
            updated_at=_now(),
        )
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    req_id = result.scalar_one_or_none()
    await db.commit()
    return None if req_id is None else await _reload(db, req_id)


async def release_claim(db: AsyncSession, req: CashOutRequest) -> None:
    """Hand the row back so a retry can claim it. Only safe when nothing was signed."""
    req.burn_started_at = None
    db.add(req)
    await db.commit()


async def complete_burn(
    db: AsyncSession,
    req: CashOutRequest,
    tx_hash: str,
    from_statuses: Iterable[CashOutStatus] = (CashOutStatus.approved,),
) -> bool:
    """Mark completed and write the simulated fiat payout — once (FR-CO-03/05).

    The balance is deliberately not touched: the UCTUSD left the wallet when the
    admin approved, and the burn has now destroyed it on-ledger. The conditional
    update makes a second call (a redelivery, or a reconcile racing the worker)
    a no-op.
    """
    now = _now()
    result = await db.execute(
        update(CashOutRequest)
        .where(CashOutRequest.id == req.id, CashOutRequest.status.in_(list(from_statuses)))
        .values(
            status=CashOutStatus.completed,
            xrpl_burn_tx_hash=tx_hash,
            failure_reason=None,
            completed_at=now,
            fiat_payout_reference=_payout_reference(req),
            fiat_paid_at=now,
            updated_at=now,
        )
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # already resolved — nothing changed
        return False
    await db.commit()
    await db.refresh(req)
    logger.info("Cash-out %s completed; burn %s", req.id, tx_hash)
    return True


async def fail_and_restore(
    db: AsyncSession,
    req: CashOutRequest,
    reason: str,
    tx_hash: Optional[str] = None,
    from_statuses: Iterable[CashOutStatus] = (CashOutStatus.approved,),
) -> bool:
    """Mark failed and restore the reserved UCTUSD — once (FR-CO-06).

    Call this only when the tokens provably did NOT leave the wallet: a validated
    tec* result, or a failure before anything was signed. The restore lives inside
    the conditional transition, so it can fire at most once no matter how many
    callers race. Restores only ever add, so the balance cannot go negative.
    """
    values = {
        "status": CashOutStatus.failed,
        "failure_reason": reason,
        "updated_at": _now(),
    }
    if tx_hash:
        values["xrpl_burn_tx_hash"] = tx_hash

    result = await db.execute(
        update(CashOutRequest)
        .where(CashOutRequest.id == req.id, CashOutRequest.status.in_(list(from_statuses)))
        .values(**values)
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # someone else resolved it; do NOT restore a second time
        return False

    wallet = (
        await db.execute(select(Wallet).where(Wallet.id == req.wallet_id).with_for_update())
    ).scalar_one()
    wallet.balance_uctusd = Decimal(wallet.balance_uctusd) + Decimal(req.uctusd_amount)
    db.add(wallet)
    await db.commit()
    await db.refresh(req)
    logger.warning("Cash-out %s failed (%s); restored %s UCTUSD", req.id, reason, req.uctusd_amount)
    return True


async def hold_unknown(db: AsyncSession, req: CashOutRequest, reason: str) -> None:
    """The burn may or may not have landed. Hold the reserve and wait for a human.

    Restoring here could credit back UCTUSD that is already destroyed on-ledger,
    so the row stays `approved`, keeps its hash and ledger range, and is shown as
    "Awaiting ledger confirmation" until an admin reconciles it against the ledger.
    """
    result = await db.execute(
        update(CashOutRequest)
        .where(CashOutRequest.id == req.id, CashOutRequest.status == CashOutStatus.approved)
        .values(failure_reason=f"{UNKNOWN_PREFIX}: {reason}", updated_at=_now())
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.scalar_one_or_none() is not None:
        await db.refresh(req)
        logger.error("Cash-out %s outcome unknown: %s — balance stays debited", req.id, reason)


async def _retry_or_fail(
    db: AsyncSession, req: CashOutRequest, reason: str, requeue: Requeue
) -> str:
    """Only for failures that provably never reached the ledger."""
    if req.burn_attempts >= MAX_ATTEMPTS:
        await fail_and_restore(db, req, f"retries_exhausted: {reason}")
        return "failed"

    delay = RETRY_DELAYS[min(req.burn_attempts - 1, len(RETRY_DELAYS) - 1)]
    await release_claim(db, req)
    requeue(delay, str(req.idempotency_key), str(req.id))
    logger.info("Re-queued cash-out %s in %ss (attempt %s)", req.id, delay, req.burn_attempts)
    return "requeued"


async def process_burn(
    idempotency_key: uuid.UUID,
    session_factory: async_sessionmaker,
    requeue: Requeue = queue_service.enqueue_burn_in,
) -> str:
    """Burn one cash-out. Returns skipped | completed | failed | requeued | unknown."""
    async with session_factory() as db:
        req = await claim(db, idempotency_key)
        if req is None:
            logger.info("Cash-out %s not claimable (duplicate, signed, or not approved)", idempotency_key)
            return "skipped"

        wallet = await db.get(Wallet, req.wallet_id)
        if wallet is None:
            await fail_and_restore(db, req, "wallet_missing: no XRPL wallet to burn from")
            return "failed"

        signed_hash: Optional[str] = None

        async def remember_signed(signed: xrpl_service.SignedTx) -> None:
            """Persist hash + ledger range before the payment is submitted.

            If this raises, xrpl_service never submits, so the burn does not exist
            and the row (still without a hash) stays claimable for a clean retry.
            """
            nonlocal signed_hash
            req.xrpl_burn_tx_hash = signed.tx_hash
            req.burn_last_ledger_sequence = signed.last_ledger_sequence
            req.burn_submitted_ledger_index = signed.submitted_ledger_index
            db.add(req)
            await db.commit()
            signed_hash = signed.tx_hash

        try:
            result = await xrpl_service.burn_to_issuer(
                wallet, Decimal(req.uctusd_amount), on_signed_tx=remember_signed
            )
        except Exception as exc:  # noqa: BLE001 — every path must leave a known state
            if signed_hash:
                # Signed and persisted, then we lost sight of it. Unknowable here.
                await hold_unknown(db, req, f"{type(exc).__name__} after signing")
                return "unknown"
            # Nothing was signed (or persistence failed, which prevents submission).
            return await _retry_or_fail(db, req, f"{type(exc).__name__}: {exc}", requeue)

        if result.success:
            await complete_burn(db, req, result.tx_hash)
            return "completed"

        if result.result_code == "sign_error":
            # Never reached the ledger.
            return await _retry_or_fail(db, req, f"sign_error: {result.message}", requeue)

        if result.tx_hash and result.result_code == "submission_error":
            # Submitted, but no validated result came back — indistinguishable from
            # a burn that succeeded. Hold, do not restore.
            await hold_unknown(db, req, f"submission_error: {result.message}")
            return "unknown"

        # A validated tec* result: the ledger says the tokens did not move.
        await fail_and_restore(
            db, req, f"{result.result_code}: {result.message}".strip(": "), result.tx_hash
        )
        return "failed"


async def _run(idempotency_key: uuid.UUID) -> str:
    # A fresh engine per job, as in settlement_worker: RQ runs each job in its own
    # event loop and asyncpg connections cannot cross loops.
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
        return await process_burn(idempotency_key, factory)
    finally:
        await engine.dispose()


def burn(idempotency_key: str) -> str:
    """RQ entrypoint (sync). Enqueued by queue_service.enqueue_burn."""
    return asyncio.run(_run(uuid.UUID(idempotency_key)))
