"""
FR-CI-01..05  Remittance creation, simulated card cash-in, and settlement hand-off.

State machine (per transaction):
  cash-in:     pending -> received | failed            (admin / mock payment service)
  settlement:  not_queued -> queued -> processing -> completed | failed   (worker)
A settlement message is published only after cash-in is received (FR-CI-03).
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.transaction import CashInStatus, SettlementStatus, Transaction
from app.models.user import KYCStatus, User
from app.services import queue_service, xrpl_service
from app.services.beneficiary_service import get_beneficiary, refresh_recipient_link
from app.services.fx_service import quote_for
from app.services.limit_service import check_limit
from app.workers.settlement_worker import complete_settlement

logger = logging.getLogger(__name__)

Enqueue = Callable[[str, str], object]

# Mock processor: this test card is always declined, so FR-CI-04 can be demoed.
DECLINED_TEST_CARD = "4000000000000002"
# An xrpl-py payment expires ~20 ledgers (~1-2 min) after signing. Past this age, a
# hash the ledger has never validated can no longer land, so a retry is safe.
LEDGER_EXPIRY_MARGIN = timedelta(minutes=5)


class RemittanceError(ValueError):
    """A send or cash-in action was refused; the message is safe to show the user."""


@dataclass(frozen=True)
class MockCard:
    number: str
    expiry: str  # MM/YY
    cvv: str
    name: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def validate_mock_card(card: MockCard) -> str:
    """Basic shape checks on simulated card details; returns the last 4 digits.

    Nothing is charged and the full number is never stored (FR-CI-01).
    """
    digits = card.number.replace(" ", "").replace("-", "")
    if not digits.isdigit() or not 13 <= len(digits) <= 19 or not _luhn_ok(digits):
        raise RemittanceError("Card number is not valid. Use a test card such as 4242 4242 4242 4242.")
    try:
        month, year = card.expiry.strip().split("/")
        month, year = int(month), 2000 + int(year)
        if not 1 <= month <= 12:
            raise ValueError
    except ValueError:
        raise RemittanceError("Expiry must be in MM/YY format.") from None
    today = _now()
    if (year, month) < (today.year, today.month):
        raise RemittanceError("This card has expired.")
    if not card.cvv.isdigit() or len(card.cvv) not in (3, 4):
        raise RemittanceError("CVV must be 3 or 4 digits.")
    if not card.name.strip():
        raise RemittanceError("Enter the name on the card.")
    return digits[-4:]


async def create_remittance(
    db: AsyncSession,
    sender: User,
    *,
    beneficiary_id: uuid.UUID,
    zar_amount: Decimal,
    card: MockCard,
    expected_exchange_rate: Optional[Decimal] = None,
) -> Transaction:
    """Insert a remittance from a confirmed quote and a simulated card payment.

    The sender's row is locked first, so the limit check and the insert run in one
    DB transaction that concurrent sends by the same user must queue behind.
    """
    if not sender.can_send or sender.kyc_status != KYCStatus.approved:
        raise RemittanceError("Your account must be KYC-approved before sending money.")

    # Held until this session's transaction ends (the commit below, or the end of
    # the request if a check fails).
    await db.execute(select(User.id).where(User.id == sender.id).with_for_update())

    beneficiary = await get_beneficiary(db, beneficiary_id, sender.id)
    if beneficiary is None:
        raise RemittanceError("Beneficiary not found.")

    recipient_id = await refresh_recipient_link(db, beneficiary)
    if recipient_id is None:
        raise RemittanceError(
            f"{beneficiary.full_name} does not have an account yet. Ask them to register "
            "with the email or mobile number you saved, then try again."
        )

    quote = await quote_for(db, zar_amount, beneficiary.payout_currency)
    if expected_exchange_rate is not None and quote.exchange_rate != expected_exchange_rate:
        raise RemittanceError("The exchange rate changed since your quote. Please review the new quote.")

    limit = await check_limit(db, sender, quote.zar_amount)
    if not limit["allowed"]:
        raise RemittanceError(limit["reason"])

    last4 = validate_mock_card(card)

    txn = Transaction(
        id=uuid.uuid4(),
        sender_id=sender.id,
        beneficiary_id=beneficiary.id,
        recipient_user_id=recipient_id,
        zar_amount=quote.zar_amount,
        transaction_fee=quote.transaction_fee,
        net_zar_converted=quote.net_zar_converted,
        market_rate=quote.market_rate,
        exchange_rate=quote.exchange_rate,
        fx_margin=quote.fx_margin,
        uctusd_amount=quote.uctusd_amount,
        idempotency_key=uuid.uuid4(),
        card_last4=last4,
    )
    if card.number.replace(" ", "").replace("-", "") == DECLINED_TEST_CARD:
        txn.cashin_status = CashInStatus.failed
        txn.cashin_failure_reason = "Card declined by the payment processor (test card)."
        txn.cashin_updated_at = _now()

    db.add(txn)
    await db.commit()
    await db.refresh(txn)
    logger.info("Created transaction %s (cash-in %s)", txn.id, txn.cashin_status.value)
    return txn


async def _publish(db: AsyncSession, txn: Transaction, enqueue: Enqueue) -> None:
    """Publish the settlement message; a queue outage fails visibly, never silently."""
    try:
        enqueue(str(txn.idempotency_key), str(txn.id))
    except Exception as exc:  # noqa: BLE001 — Redis down, etc.
        txn.settlement_status = SettlementStatus.failed
        txn.xrpl_error_reason = f"queue_unavailable: {type(exc).__name__}; retry from the admin monitor"
        db.add(txn)
        await db.commit()
        logger.error("Could not enqueue settlement for %s: %s", txn.id, exc)


async def mark_cashin_received(
    db: AsyncSession,
    txn: Transaction,
    reviewer: Optional[User],
    enqueue: Enqueue = queue_service.enqueue_settlement,
) -> Transaction:
    """FR-CI-02/03, FR-MQ-01: confirm the card payment and queue settlement.

    The conditional update means a double-click or repeated API call cannot
    publish a second message.
    """
    result = await db.execute(
        update(Transaction)
        .where(Transaction.id == txn.id, Transaction.cashin_status == CashInStatus.pending)
        .values(
            cashin_status=CashInStatus.received,
            cashin_updated_at=_now(),
            cashin_reviewed_by=reviewer.id if reviewer else None,
            settlement_status=SettlementStatus.queued,
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # nothing changed; commit (not rollback) keeps loaded objects usable
        raise RemittanceError("This cash-in has already been processed.")
    await db.commit()
    await db.refresh(txn)

    # Commit first, then publish: the worker only claims rows already marked queued.
    await _publish(db, txn, enqueue)
    return txn


async def mark_cashin_failed(
    db: AsyncSession, txn: Transaction, reviewer: Optional[User], reason: str = ""
) -> Transaction:
    """FR-CI-02/04: payment not received — the transaction stops; nothing is sent."""
    result = await db.execute(
        update(Transaction)
        .where(Transaction.id == txn.id, Transaction.cashin_status == CashInStatus.pending)
        .values(
            cashin_status=CashInStatus.failed,
            cashin_updated_at=_now(),
            cashin_reviewed_by=reviewer.id if reviewer else None,
            cashin_failure_reason=reason.strip() or "Payment was not received.",
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # nothing changed; commit (not rollback) keeps loaded objects usable
        raise RemittanceError("This cash-in has already been processed.")
    await db.commit()
    await db.refresh(txn)
    return txn


STUCK_AFTER = timedelta(minutes=10)  # > JOB_TIMEOUT, so no live worker still owns the row


def _is_stuck(txn: Transaction) -> bool:
    return (
        txn.settlement_status == SettlementStatus.processing
        and _now() - txn.updated_at >= STUCK_AFTER
    )


async def retry_settlement(
    db: AsyncSession,
    txn: Transaction,
    enqueue: Enqueue = queue_service.enqueue_settlement,
    ledger_result=xrpl_service.get_transaction_result,
) -> str:
    """FR-MQ-06: admin retry of a failed or stuck settlement. Returns "requeued" or "reconciled".

    Covers a `failed` row, and a `processing` row whose worker died (older than
    STUCK_AFTER). If an attempt was signed, the ledger is checked first: a payment
    that actually succeeded is recorded (never re-sent), and one that may still
    land is refused until it has expired.
    """
    retryable = txn.settlement_status == SettlementStatus.failed or _is_stuck(txn)
    if not retryable or txn.cashin_status != CashInStatus.received:
        raise RemittanceError("Only failed or stuck settlements can be retried.")
    from_status, seen_at = txn.settlement_status, txn.updated_at

    if txn.xrpl_tx_hash:
        code = await ledger_result(txn.xrpl_tx_hash)
        if code == xrpl_service.SUCCESS:
            await complete_settlement(db, txn, txn.xrpl_tx_hash, from_statuses=(from_status,))
            return "reconciled"
        if code is None and _now() - txn.updated_at < LEDGER_EXPIRY_MARGIN:
            raise RemittanceError(
                "The last attempt is not final on the ledger yet. Wait a few minutes and retry."
            )

    # Guarded on status + updated_at: if anything touched the row since it was
    # read, nothing changes and the admin is asked to refresh.
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.id == txn.id,
            Transaction.settlement_status == from_status,
            Transaction.updated_at == seen_at,
        )
        .values(
            settlement_status=SettlementStatus.queued,
            settlement_attempts=0,
            xrpl_error_reason=None,
            xrpl_tx_hash=None,
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    if result.scalar_one_or_none() is None:
        await db.commit()  # nothing changed; commit (not rollback) keeps loaded objects usable
        raise RemittanceError("This settlement changed while you were looking at it. Refresh and try again.")
    await db.commit()
    await db.refresh(txn)
    await _publish(db, txn, enqueue)
    return "requeued"


async def requeue_stuck(
    db: AsyncSession, txn: Transaction, enqueue: Enqueue = queue_service.enqueue_settlement
) -> None:
    """Re-publish a message for a row still `queued` (e.g. its message was lost).

    Always safe: the worker's claim ignores anything no longer queued.
    """
    if txn.settlement_status != SettlementStatus.queued:
        raise RemittanceError("Only queued settlements can be re-queued.")
    await _publish(db, txn, enqueue)


# --- queries ---

def _with_parties(stmt):
    return stmt.options(
        selectinload(Transaction.beneficiary),
        selectinload(Transaction.sender),
        selectinload(Transaction.recipient),
    )


async def get_transaction(db: AsyncSession, txn_id: uuid.UUID) -> Optional[Transaction]:
    result = await db.execute(_with_parties(select(Transaction).where(Transaction.id == txn_id)))
    return result.scalar_one_or_none()


async def list_for_sender(db: AsyncSession, sender_id: uuid.UUID, limit: Optional[int] = None) -> list[Transaction]:
    stmt = _with_parties(
        select(Transaction).where(Transaction.sender_id == sender_id).order_by(Transaction.created_at.desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def list_incoming(db: AsyncSession, recipient_id: uuid.UUID) -> list[Transaction]:
    """Transfers to this recipient that have reached the settlement stage (FR-WAL-05)."""
    stmt = _with_parties(
        select(Transaction)
        .where(
            Transaction.recipient_user_id == recipient_id,
            Transaction.settlement_status != SettlementStatus.not_queued,
        )
        .order_by(Transaction.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_pending_cashins(db: AsyncSession) -> list[Transaction]:
    stmt = _with_parties(
        select(Transaction)
        .where(Transaction.cashin_status == CashInStatus.pending)
        .order_by(Transaction.created_at.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def list_settlement_issues(db: AsyncSession) -> list[Transaction]:
    """FR-MQ-06, FR-ADM-05: failed settlements, plus any stuck queued/processing."""
    stuck_before = _now() - STUCK_AFTER
    stmt = _with_parties(
        select(Transaction)
        .where(
            (Transaction.settlement_status == SettlementStatus.failed)
            | (
                Transaction.settlement_status.in_([SettlementStatus.queued, SettlementStatus.processing])
                & (Transaction.updated_at < stuck_before)
            )
        )
        .order_by(Transaction.updated_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())
