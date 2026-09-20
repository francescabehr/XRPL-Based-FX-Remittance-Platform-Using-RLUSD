"""
FR-CO-01..06  Cash-out: UCTUSD -> fiat, burned on-ledger, payout simulated in the DB.

State machine (forward-only, FR-CO-03):

    requested --approve--> approved --burn ok------> completed
        |                     |    \--burn tec*---> failed   (+restore)
        |                     |     \--unknown----> stays approved, held
        \--reject-----------> failed  (no debit ever happened)

Where the money moves:
  - approve       : balance_uctusd -= uctusd_amount   (the reserve, FR-CO-06)
  - approved->failed: balance_uctusd += uctusd_amount (restored exactly once)
  - nothing else touches the balance. The debit happens under a FOR UPDATE lock
    behind a balance check, and restores only add, so it can never go negative.

Locks are held only across DB work — never across an XRPL round trip.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.beneficiary import PayoutCurrency
from app.models.cashout import CashOutRequest, CashOutStatus
from app.models.user import User
from app.models.wallet import Wallet
from app.services import fx_service, queue_service, xrpl_service
from app.services.fx_service import QUANT_UCTUSD, cashout_fee_usd, cashout_payout
from app.workers.cashout_worker import UNKNOWN_PREFIX, complete_burn, fail_and_restore

logger = logging.getLogger(__name__)

Enqueue = Callable[[str, str], object]

# How long an approved-but-unpublished row may sit before the sweep re-publishes it.
# Covers the commit-before-publish window: approval commits, then the process dies
# before the message reaches Redis.
PUBLISH_GRACE = timedelta(minutes=2)


class CashOutError(ValueError):
    """A cash-out action was refused; the message is safe to show the user."""


class PricingChanged(CashOutError):
    """Fees or the rate moved between preview and submit — a fresh preview is required."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CashOutPricing:
    """What the recipient is shown before submitting, and what gets persisted."""

    uctusd_amount: Decimal
    target_currency: PayoutCurrency
    market_rate: Decimal
    cashout_fee_percentage: Decimal
    cashout_fee_min_usd: Decimal
    cashout_fee_usd: Decimal
    net_payout: Decimal


def parse_amount(raw: str) -> Decimal:
    """Parse a user-entered UCTUSD amount to 6 dp, or refuse it."""
    try:
        amount = Decimal(str(raw).strip().replace(",", ""))
    except (InvalidOperation, AttributeError, ValueError):
        raise CashOutError("Enter a valid UCTUSD amount.") from None
    if not amount.is_finite() or amount <= 0:
        raise CashOutError("Enter a cash-out amount greater than zero.")
    return amount.quantize(QUANT_UCTUSD)


def parse_currency(raw: str) -> PayoutCurrency:
    """USD and ZAR are the only supported cash-out currencies (FR-CO-01)."""
    try:
        return PayoutCurrency(str(raw).strip().upper())
    except ValueError:
        raise CashOutError("Cash-out is available in USD or ZAR only.") from None


async def price_cashout(
    db: AsyncSession, amount: Decimal, currency: PayoutCurrency
) -> CashOutPricing:
    """Current terms for a cash-out, using the shared fx_service math (FR-CO-02)."""
    fee_config = await fx_service.get_active_fee_config(db)
    if fee_config is None:
        raise CashOutError("Cash-out is unavailable right now. Please try again later.")

    market_rate = await fx_service.get_market_rate(db, fee_config)
    fee_usd = cashout_fee_usd(amount, fee_config.cashout_fee_percentage, fee_config.cashout_fee_min_usd)
    payout = cashout_payout(amount, fee_usd, market_rate, currency)

    if payout <= 0:
        raise CashOutError(
            f"That amount is too small: the cash-out fee of ${fee_usd:,.6f} would leave "
            "nothing to pay out. Try a larger amount."
        )

    return CashOutPricing(
        uctusd_amount=amount,
        target_currency=currency,
        market_rate=market_rate,
        cashout_fee_percentage=Decimal(fee_config.cashout_fee_percentage),
        cashout_fee_min_usd=Decimal(fee_config.cashout_fee_min_usd),
        cashout_fee_usd=fee_usd,
        net_payout=payout,
    )


async def held_for_requests(db: AsyncSession, recipient_id: uuid.UUID) -> Decimal:
    """UCTUSD committed to cash-outs that are requested but not yet approved.

    `approved` rows are deliberately excluded: their amount already left
    balance_uctusd at approval, so counting them again would double-subtract.
    """
    total = (
        await db.execute(
            select(func.coalesce(func.sum(CashOutRequest.uctusd_amount), 0)).where(
                CashOutRequest.recipient_user_id == recipient_id,
                CashOutRequest.status == CashOutStatus.requested,
            )
        )
    ).scalar_one()
    return Decimal(total)


async def available_balance(db: AsyncSession, recipient_id: uuid.UUID) -> Decimal:
    """Balance the recipient may still commit: cache minus outstanding requests."""
    wallet = await xrpl_service.get_wallet_for_user(db, recipient_id)
    if wallet is None:
        return Decimal("0")
    return Decimal(wallet.balance_uctusd) - await held_for_requests(db, recipient_id)


def _pricing_matches(pricing: CashOutPricing, accepted: Optional[CashOutPricing]) -> bool:
    if accepted is None:
        return True
    return (
        pricing.market_rate == accepted.market_rate
        and pricing.cashout_fee_usd == accepted.cashout_fee_usd
        and pricing.net_payout == accepted.net_payout
    )


async def create_request(
    db: AsyncSession,
    recipient: User,
    *,
    amount: Decimal,
    currency: PayoutCurrency,
    accepted: Optional[CashOutPricing] = None,
) -> CashOutRequest:
    """FR-CO-01: open a cash-out against the recipient's available balance.

    The wallet row is locked for the whole check-and-insert, so two concurrent
    requests cannot both pass an availability check the wallet can only satisfy
    once. `accepted` carries the figures the recipient was shown: they are only
    ever compared against a fresh server-side computation, never persisted.
    """
    wallet = (
        await db.execute(
            select(Wallet).where(Wallet.user_id == recipient.id).with_for_update()
        )
    ).scalar_one_or_none()
    if wallet is None:
        raise CashOutError("You do not have a UCTUSD wallet yet.")

    available = Decimal(wallet.balance_uctusd) - await held_for_requests(db, recipient.id)
    if amount > available:
        raise CashOutError(
            f"You can cash out at most {available:,.6f} UCTUSD right now "
            f"(balance {Decimal(wallet.balance_uctusd):,.6f}, less cash-outs awaiting approval)."
        )

    # Priced server-side, now, under the lock. The client's figures never land in the row.
    pricing = await price_cashout(db, amount, currency)
    if not _pricing_matches(pricing, accepted):
        raise PricingChanged(
            "The rate or fee changed while you were reviewing. Check the updated payout and submit again."
        )

    req = CashOutRequest(
        id=uuid.uuid4(),
        recipient_user_id=recipient.id,
        wallet_id=wallet.id,
        uctusd_amount=pricing.uctusd_amount,
        target_currency=pricing.target_currency,
        market_rate=pricing.market_rate,
        cashout_fee_percentage=pricing.cashout_fee_percentage,
        cashout_fee_min_usd=pricing.cashout_fee_min_usd,
        cashout_fee_usd=pricing.cashout_fee_usd,
        net_payout=pricing.net_payout,
        status=CashOutStatus.requested,
        idempotency_key=uuid.uuid4(),
    )
    db.add(req)
    await db.commit()
    await db.refresh(req)
    logger.info("Cash-out %s requested: %s UCTUSD -> %s", req.id, req.uctusd_amount, currency.value)
    return req


async def _publish(db: AsyncSession, req: CashOutRequest, enqueue: Enqueue) -> None:
    """Publish the burn message; a queue outage fails visibly and restores the reserve."""
    try:
        enqueue(str(req.idempotency_key), str(req.id))
    except Exception as exc:  # noqa: BLE001 — Redis down, etc.
        await fail_and_restore(
            db, req, f"queue_unavailable: {type(exc).__name__}; the reserved UCTUSD was returned"
        )
        logger.error("Could not enqueue burn for cash-out %s: %s", req.id, exc)


async def approve(
    db: AsyncSession,
    req: CashOutRequest,
    admin: User,
    enqueue: Optional[Enqueue] = None,
) -> CashOutRequest:
    """FR-CO-05/06: reserve the UCTUSD and hand the burn to the queue.

    Ordering matters and is deliberate:
      1. lock the wallet   — concurrent approvals against one wallet serialise here
      2. re-check balance  — under the lock, so the second one sees the first debit
      3. claim requested->approved (conditional) — a double click changes nothing
      4. debit
      5. commit            — status and money land together, or neither does
      6. publish           — only after the row is durably approved
    Steps 1-5 are one transaction; no network call happens inside it.
    """
    # Resolved here, not in the signature: a default argument would bind the
    # function object at import time and ignore any later substitution.
    enqueue = enqueue or queue_service.enqueue_burn

    wallet = (
        await db.execute(
            select(Wallet).where(Wallet.id == req.wallet_id).with_for_update()
        )
    ).scalar_one_or_none()
    if wallet is None:
        raise CashOutError("The recipient's wallet is missing; this cash-out cannot be approved.")

    amount = Decimal(req.uctusd_amount)
    if Decimal(wallet.balance_uctusd) < amount:
        raise CashOutError(
            f"Balance is only {Decimal(wallet.balance_uctusd):,.6f} UCTUSD — not enough to "
            f"reserve {amount:,.6f}. The recipient may have cashed out since this was requested."
        )

    result = await db.execute(
        update(CashOutRequest)
        .where(CashOutRequest.id == req.id, CashOutRequest.status == CashOutStatus.requested)
        .values(
            status=CashOutStatus.approved,
            approved_by=admin.id,
            approved_at=_now(),
            updated_at=_now(),
        )
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # nothing changed; commit keeps loaded objects usable
        raise CashOutError("This cash-out has already been actioned.")

    wallet.balance_uctusd = Decimal(wallet.balance_uctusd) - amount
    db.add(wallet)
    await db.commit()
    await db.refresh(req)
    logger.info("Cash-out %s approved by %s; reserved %s UCTUSD", req.id, admin.id, amount)

    await _publish(db, req, enqueue)
    return req


async def reject(
    db: AsyncSession, req: CashOutRequest, admin: User, reason: str = ""
) -> CashOutRequest:
    """FR-CO-05: refuse a cash-out before approval. No debit has happened, so none is reversed."""
    result = await db.execute(
        update(CashOutRequest)
        .where(CashOutRequest.id == req.id, CashOutRequest.status == CashOutStatus.requested)
        .values(
            status=CashOutStatus.failed,
            approved_by=admin.id,
            approved_at=_now(),
            failure_reason=reason.strip() or "Rejected by an administrator.",
            updated_at=_now(),
        )
        .returning(CashOutRequest.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()
        raise CashOutError("This cash-out has already been actioned.")
    await db.commit()
    await db.refresh(req)
    return req


async def reconcile(
    db: AsyncSession,
    req: CashOutRequest,
    ledger_result=xrpl_service.get_transaction_result,
    latest_ledger=xrpl_service.get_latest_validated_ledger,
    ledger_range=xrpl_service.has_complete_ledger_range,
) -> str:
    """Resolve a held cash-out from the ledger. Returns completed | failed | unresolved.

    Only ledger evidence moves this row. Following XRPL's reliable transaction
    submission guidance, a transaction is proven dead only when BOTH hold:
      - the current validated ledger is past its LastLedgerSequence, and
      - the server has unbroken history across [submitted, LastLedgerSequence],
        so "not found" means "was never included", not "cannot see it".
    Elapsed wall-clock time is never treated as evidence.

    Racing an active worker is safe: the worker cannot resubmit a row that already
    has a hash, and both sides finalise through conditional updates, so whichever
    lands first wins and the other becomes a no-op.
    """
    if req.status in (CashOutStatus.completed, CashOutStatus.failed):
        raise CashOutError("This cash-out is already resolved.")
    if req.status != CashOutStatus.approved or not req.xrpl_burn_tx_hash:
        raise CashOutError("There is no submitted burn to reconcile yet.")

    code = await ledger_result(req.xrpl_burn_tx_hash)
    if code == xrpl_service.SUCCESS:
        await complete_burn(db, req, req.xrpl_burn_tx_hash)
        return "completed"
    if code is not None:
        # A validated non-success result: the ledger says the tokens never moved.
        await fail_and_restore(db, req, f"{code}: burn rejected on-ledger", req.xrpl_burn_tx_hash)
        return "failed"

    # Not found. Absence is only proof if the transaction can no longer be included.
    last_ledger = req.burn_last_ledger_sequence
    submitted_at = req.burn_submitted_ledger_index
    if last_ledger is None or submitted_at is None:
        return "unresolved"

    if await latest_ledger() <= last_ledger:
        return "unresolved"  # could still be validated
    if not await ledger_range(submitted_at, last_ledger):
        return "unresolved"  # a gap in history: "missing" proves nothing

    await fail_and_restore(
        db,
        req,
        f"expired: not validated by ledger {last_ledger}; the burn can never be included",
        req.xrpl_burn_tx_hash,
    )
    return "failed"


async def sweep_unpublished(
    db: AsyncSession,
    enqueue: Optional[Enqueue] = None,
    grace: timedelta = PUBLISH_GRACE,
) -> list[uuid.UUID]:
    """Re-publish burns lost in the commit-before-publish window.

    Approval commits the debit and then publishes. If the process dies in between,
    the row is approved and reserved but no message exists, and nothing else would
    ever pick it up. This finds those rows — approved, never signed, never claimed,
    and older than the grace period — and re-publishes them.

    Safe to run repeatedly: the worker's claim is what enforces exactly-once, so a
    duplicate message is a no-op. Run at startup and periodically.
    """
    enqueue = enqueue or queue_service.enqueue_burn
    cutoff = _now() - grace
    rows = (
        await db.execute(
            select(CashOutRequest).where(
                CashOutRequest.status == CashOutStatus.approved,
                CashOutRequest.xrpl_burn_tx_hash.is_(None),
                CashOutRequest.burn_started_at.is_(None),
                CashOutRequest.approved_at < cutoff,
            )
        )
    ).scalars().all()

    republished: list[uuid.UUID] = []
    for req in rows:
        try:
            enqueue(str(req.idempotency_key), str(req.id))
        except Exception as exc:  # noqa: BLE001 — leave it for the next sweep
            logger.error("Sweep could not re-publish cash-out %s: %s", req.id, exc)
            continue
        republished.append(req.id)
        logger.warning("Sweep re-published unpublished burn for cash-out %s", req.id)
    return republished


# --- queries ---

def _with_parties(stmt):
    # populate_existing: the burn worker updates these rows in its own session, so
    # a cached instance could otherwise show a status that is already out of date.
    return stmt.options(
        selectinload(CashOutRequest.recipient),
        selectinload(CashOutRequest.wallet),
        selectinload(CashOutRequest.approver),
    ).execution_options(populate_existing=True)


async def get_request(
    db: AsyncSession, req_id: uuid.UUID, recipient_id: Optional[uuid.UUID] = None
) -> Optional[CashOutRequest]:
    """One cash-out. Pass recipient_id to enforce ownership for recipient-facing routes."""
    stmt = _with_parties(select(CashOutRequest).where(CashOutRequest.id == req_id))
    if recipient_id is not None:
        stmt = stmt.where(CashOutRequest.recipient_user_id == recipient_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_for_recipient(db: AsyncSession, recipient_id: uuid.UUID) -> list[CashOutRequest]:
    """FR-CO-04: the recipient's cash-out history, newest first."""
    stmt = _with_parties(
        select(CashOutRequest)
        .where(CashOutRequest.recipient_user_id == recipient_id)
        .order_by(CashOutRequest.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_open(db: AsyncSession) -> list[CashOutRequest]:
    """Admin queue: everything still awaiting an admin decision or a ledger outcome."""
    stmt = _with_parties(
        select(CashOutRequest)
        .where(CashOutRequest.status.in_([CashOutStatus.requested, CashOutStatus.approved]))
        .order_by(CashOutRequest.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())
