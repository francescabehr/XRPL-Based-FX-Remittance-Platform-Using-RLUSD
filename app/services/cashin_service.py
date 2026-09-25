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
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.beneficiary import Beneficiary
from app.models.transaction import CashInStatus, SettlementStatus, Transaction
from app.models.user import KYCStatus, User
from app.services import queue_service, xrpl_service
from app.services.beneficiary_service import get_beneficiary, refresh_recipient_link
from app.services.fx_service import AmountTooSmall, quote_for
from app.services.limit_service import check_limit, day_start_utc
from app.workers.settlement_worker import complete_settlement, fail_settlement

logger = logging.getLogger(__name__)

Enqueue = Callable[[str, str], object]

# Mock processor: this test card is always declined, so FR-CI-04 can be demoed.
DECLINED_TEST_CARD = "4000000000000002"

# How long a cash-in may sit `pending` before it is abandoned (FR-LIM-01/02).
#
# A pending row holds the sender's daily and monthly allowance — in-flight rows
# must count, or two concurrent sends would each see the same headroom (the
# TOCTOU hole limit_service exists to close). Without an expiry, a send that is
# never confirmed holds that allowance forever.
#
# Hours, not minutes: `pending` means "awaiting confirmation from the payment
# processor", which in this build is an admin working the cash-in queue. A short
# window would cancel perfectly good transactions waiting on a human. This is the
# analogue of a card authorisation that is never captured.
CASHIN_EXPIRY = timedelta(hours=24)


class RemittanceError(ValueError):
    """A send or cash-in action was refused; the message is safe to show the user."""


@dataclass(frozen=True)
class AcceptedQuote:
    """The pricing the sender was shown and is agreeing to pay.

    Compared against a fresh server-side quote, never persisted from the client.
    All three figures are checked, not just the rate: a fee-only config change
    leaves the effective rate identical while changing what the sender pays, so a
    rate-only comparison would let it through (the cash-out side already compares
    its full set — see cashout_service._pricing_matches).
    """

    exchange_rate: Decimal
    transaction_fee: Decimal
    uctusd_amount: Decimal


def _pricing_matches(quote, accepted: AcceptedQuote) -> bool:
    return (
        quote.exchange_rate == accepted.exchange_rate
        and quote.transaction_fee == accepted.transaction_fee
        and quote.uctusd_amount == accepted.uctusd_amount
    )


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
    accepted: AcceptedQuote,
) -> Transaction:
    """Insert a remittance from a confirmed quote and a simulated card payment.

    The sender's row is locked first, so the limit check and the insert run in one
    DB transaction that concurrent sends by the same user must queue behind.

    `accepted` is the pricing the sender was shown, and it is mandatory
    (requirements.md §382): the quote is re-priced server-side here and refused
    unless it still matches, so the sender pays the price they saw or is sent back
    to a fresh quote. It is a refusal, never something a caller can opt out of by
    omitting a field — the figures themselves are never persisted from the client.
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

    # Re-priced server-side: the floor is enforced here too, so a hand-made POST
    # cannot book a send the quote screen would have refused.
    try:
        quote = await quote_for(db, zar_amount, beneficiary.payout_currency)
    except AmountTooSmall as exc:
        raise RemittanceError(str(exc)) from exc
    if not _pricing_matches(quote, accepted):
        raise RemittanceError(
            "The price changed since your quote. Please review the new quote before paying."
        )

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
    """Publish the settlement message; a queue outage fails visibly, never silently.

    The failure write is conditional on the row still being `queued`. Redis can
    accept a job and still raise (a timeout on the response, say), so by the time
    this runs a worker may already have claimed the row and moved it to
    `processing`. Stamping `failed` over that worker used to leave the payment
    landing on-ledger while complete_settlement's conditional matched nothing —
    the recipient was never credited.
    """
    try:
        enqueue(str(txn.idempotency_key), str(txn.id))
    except Exception as exc:  # noqa: BLE001 — Redis down, etc.
        logger.error("Could not enqueue settlement for %s: %s", txn.id, exc)
        await fail_settlement(
            db,
            txn,
            f"queue_unavailable: {type(exc).__name__}; retry from the admin monitor",
            from_statuses=(SettlementStatus.queued,),
        )


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


async def expire_stale_cashins(
    db: AsyncSession, older_than: timedelta = CASHIN_EXPIRY
) -> int:
    """Fail cash-ins that were never confirmed, releasing the allowance they hold.

    One conditional UPDATE, guarded on the expected state: only `pending` rows
    older than the window move, so a cash-in confirmed a moment ago is untouched
    no matter how often this runs. Nothing on-chain has happened for these rows —
    a pending cash-in is never queued for settlement — so failing them moves no
    money; it only stops them consuming limit allowance indefinitely.

    Returns how many were expired. Safe to run repeatedly.
    """
    cutoff = _now() - older_than
    result = await db.execute(
        update(Transaction)
        .where(
            Transaction.cashin_status == CashInStatus.pending,
            Transaction.created_at < cutoff,
        )
        .values(
            cashin_status=CashInStatus.failed,
            cashin_failure_reason=(
                "Payment was not confirmed in time; this transfer was cancelled. "
                "Nothing was sent and your limit allowance has been released."
            ),
            cashin_updated_at=_now(),
            updated_at=_now(),
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    expired = list(result.scalars().all())
    await db.commit()
    if expired:
        logger.warning("Expired %s unconfirmed cash-in(s) older than %s", len(expired), older_than)
    return len(expired)


async def _refuse_unless_provably_dead(
    txn: Transaction,
    latest_ledger=xrpl_service.get_latest_validated_ledger,
    ledger_range=xrpl_service.has_complete_ledger_range,
) -> None:
    """Raise unless the ledger proves the signed payment can never be included.

    The sole gate on re-sending. A row whose range was never recorded (signed
    before migration 0009) can never be proven dead, so it is refused too — an
    admin resolves it by looking the hash up on the ledger.
    """
    last_ledger = txn.settlement_last_ledger_sequence
    submitted_at = txn.settlement_submitted_ledger_index
    if last_ledger is None or submitted_at is None:
        raise RemittanceError(
            "This attempt has no recorded ledger range, so it cannot be shown to have "
            f"failed. Check {txn.xrpl_tx_hash} on the ledger before retrying."
        )
    if await latest_ledger() <= last_ledger:
        raise RemittanceError(
            "The last attempt could still be validated. Wait until ledger "
            f"{last_ledger} has passed, then retry."
        )
    if not await ledger_range(submitted_at, last_ledger):
        raise RemittanceError(
            "The server is missing ledger history for this attempt, so a missing "
            "payment proves nothing. Retry once its history is complete."
        )


def _archived_attempts(txn: Transaction) -> list[dict]:
    """The attempt history with the current attempt retired onto the end.

    Hash and ledger range are archived together, never one without the other:
    a bare hash cannot be investigated, because "absent from the ledger" is only
    meaningful against the range the payment could have appeared in. Keeping the
    hash but dropping the range would re-create the ambiguity that made the
    wall-clock resend unsafe in the first place.

    Written in the same conditional UPDATE that clears the live fields, so the
    record moves rather than being destroyed.
    """
    history = list(txn.settlement_previous_attempts or [])
    if not txn.xrpl_tx_hash:
        return history  # nothing was ever signed; there is no attempt to keep
    history.append(
        {
            "tx_hash": txn.xrpl_tx_hash,
            "last_ledger_sequence": txn.settlement_last_ledger_sequence,
            "submitted_ledger_index": txn.settlement_submitted_ledger_index,
            "attempts": txn.settlement_attempts,
            "failure_reason": txn.xrpl_error_reason,
            "retired_at": _now().isoformat(),
        }
    )
    return history


async def retry_settlement(
    db: AsyncSession,
    txn: Transaction,
    enqueue: Enqueue = queue_service.enqueue_settlement,
    ledger_result=xrpl_service.get_transaction_result,
    latest_ledger=xrpl_service.get_latest_validated_ledger,
    ledger_range=xrpl_service.has_complete_ledger_range,
) -> str:
    """FR-MQ-06: admin retry of a failed or stuck settlement. Returns "requeued" or "reconciled".

    Covers a `failed` row, and a `processing` row whose worker died (older than
    STUCK_AFTER). If an attempt was signed, the ledger is checked first: a payment
    that actually succeeded is recorded and never re-sent.

    Re-sending demands ledger proof that the previous attempt is dead, exactly as
    cash-out reconcile does — both of:
      - the validated ledger is past the payment's LastLedgerSequence, and
      - the server holds unbroken history across [submitted, LastLedgerSequence],
        so "not found" means "never included", not "this node cannot see it".
    Elapsed wall-clock time is never evidence: get_transaction_result returns None
    both for a payment that was never included and for one the node simply cannot
    see, and re-sending on the second would pay the recipient twice.
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
        if code is None:
            # Absent from the ledger. That only permits a re-send once the payment
            # can no longer be included and the server can actually prove it.
            await _refuse_unless_provably_dead(txn, latest_ledger, ledger_range)

    # Guarded on status + updated_at: if anything touched the row since it was
    # read, nothing changes and the admin is asked to refresh. The archive is
    # computed from that same read, so it cannot be built from stale data either.
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
            # Cleared so the row can be claimed and signed again — and archived
            # first, hash and range together, so the retired attempt stays
            # traceable on the ledger.
            xrpl_tx_hash=None,
            settlement_last_ledger_sequence=None,
            settlement_submitted_ledger_index=None,
            settlement_previous_attempts=_archived_attempts(txn),
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
    # populate_existing, for the same reason cashout_service._with_parties uses it:
    # the settlement worker updates these rows in its own session, so a cached
    # instance in this one could otherwise render a status, hash or attempt
    # history that is already out of date.
    return stmt.options(
        selectinload(Transaction.beneficiary),
        selectinload(Transaction.sender),
        selectinload(Transaction.recipient),
    ).execution_options(populate_existing=True)


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


# --- admin transaction monitor (FR-ADM-04, FR-ADM-07) ---

# Ceiling on a single monitor page: the filters narrow the set, this stops an
# unfiltered view from loading the whole table.
MONITOR_LIMIT = 500


def _day_bounds(day: date) -> datetime:
    """The UTC instant at which `day` begins in DISPLAY_TIMEZONE.

    The admin picks a date in their own timezone and every timestamp on the page
    is rendered in it, so the filter must bound the same local day. Building UTC
    midnight instead silently dropped the first hours of each local day.
    """
    return day_start_utc(day)


async def list_transactions(
    db: AsyncSession,
    *,
    cashin_status: Optional[CashInStatus] = None,
    settlement_status: Optional[SettlementStatus] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    user_query: Optional[str] = None,
    aml_flagged: Optional[bool] = None,
    limit: int = MONITOR_LIMIT,
) -> list[Transaction]:
    """Every transaction matching the monitor's filters, newest first.

    Filters are AND-ed; a `None` means "no constraint". `user_query` matches the
    sender, the linked recipient, or the beneficiary by name or email.
    """
    stmt = _with_parties(select(Transaction))

    if cashin_status is not None:
        stmt = stmt.where(Transaction.cashin_status == cashin_status)
    if settlement_status is not None:
        stmt = stmt.where(Transaction.settlement_status == settlement_status)
    if date_from is not None:
        stmt = stmt.where(Transaction.created_at >= _day_bounds(date_from))
    if date_to is not None:
        # Inclusive of the whole end day.
        stmt = stmt.where(Transaction.created_at < _day_bounds(date_to + timedelta(days=1)))
    if aml_flagged is not None:
        stmt = stmt.where(Transaction.aml_flagged.is_(aml_flagged))

    term = (user_query or "").strip()
    if term:
        pattern = f"%{term}%"
        matches_user = or_(User.full_name.ilike(pattern), User.email.ilike(pattern))
        stmt = stmt.where(
            or_(
                select(User.id).where(User.id == Transaction.sender_id, matches_user).exists(),
                select(User.id).where(User.id == Transaction.recipient_user_id, matches_user).exists(),
                select(Beneficiary.id)
                .where(
                    Beneficiary.id == Transaction.beneficiary_id,
                    or_(Beneficiary.full_name.ilike(pattern), Beneficiary.email.ilike(pattern)),
                )
                .exists(),
            )
        )

    stmt = stmt.order_by(Transaction.created_at.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def set_aml_flag(db: AsyncSession, txn: Transaction, flagged: bool) -> Transaction:
    """FR-ADM-07: mark or clear a transaction for AML review.

    Review-only: it never touches cash-in state, settlement state or balances.
    """
    txn.aml_flagged = flagged
    await db.commit()
    await db.refresh(txn)
    return txn
