from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class CashInStatus(str, enum.Enum):
    """Sender-side funding state (FR-CI-02..05)."""

    pending = "pending"
    received = "received"
    failed = "failed"


class SettlementStatus(str, enum.Enum):
    """On-ledger settlement state, driven by the worker in Phase 6 (FR-MQ, FR-WAL)."""

    not_queued = "not_queued"
    queued = "queued"
    processing = "processing"
    completed = "completed"
    failed = "failed"


class Transaction(Base):
    """A remittance: ZAR in from the sender, UCTUSD out to the beneficiary.

    The FX figures are snapshotted onto the row at creation, so editing
    fee_config affects new quotes only and never rewrites history (FR-FX-08).
    """

    __tablename__ = "transactions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sender_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    beneficiary_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("beneficiaries.id"), nullable=False, index=True
    )
    # The registered user who receives the UCTUSD, snapshotted at creation so a later
    # beneficiary edit cannot redirect a settlement already in flight.
    recipient_user_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True, index=True
    )

    # --- money: ZAR 2 dp, UCTUSD 6 dp, rates 6 dp ---
    zar_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    transaction_fee: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    net_zar_converted: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    market_rate: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # Effective rate actually applied: market_rate * (1 + fx_margin).
    exchange_rate: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    fx_margin: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    uctusd_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)

    # Queue dedup / anti-double-credit key — the worker claims this before crediting (FR-MQ-04).
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4
    )

    cashin_status: Mapped[CashInStatus] = mapped_column(
        SAEnum(CashInStatus, name="cashinstatus"),
        nullable=False,
        default=CashInStatus.pending,
        server_default=CashInStatus.pending.value,
    )
    settlement_status: Mapped[SettlementStatus] = mapped_column(
        SAEnum(SettlementStatus, name="settlementstatus"),
        nullable=False,
        default=SettlementStatus.not_queued,
        server_default=SettlementStatus.not_queued.value,
    )

    # --- cash-in (FR-CI-01..05). Only the card's last 4 digits are ever stored. ---
    card_last4: Mapped[Optional[str]] = mapped_column(String(4), nullable=True)
    cashin_updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    cashin_reviewed_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    cashin_failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- settlement (FR-MQ, FR-WAL-05..07) ---
    # Treasury -> recipient hash. Written as soon as the payment is signed, before
    # submission, so an interrupted settlement can be checked on the ledger.
    xrpl_tx_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # The only ledger range in which that hash can ever appear, written with it.
    # A missing transaction is proven dead only when the validated ledger is past
    # last_ledger AND the server holds unbroken history across the range; without
    # both, "not found" may only mean "this node cannot see it" — so a retry that
    # re-sent on elapsed time could pay a recipient twice.
    settlement_last_ledger_sequence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settlement_submitted_ledger_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    settlement_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # Attempts retired by an admin retry, newest last. Each entry keeps the hash
    # AND its ledger range together, because a hash without its range cannot be
    # reasoned about — "not on the ledger" would again be indistinguishable from
    # "this node cannot see it". Never read by the money path; it exists so a
    # payment that may have landed is still traceable after a re-send.
    settlement_previous_attempts: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    xrpl_error_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    settled_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- AML review (FR-ADM-07). Set by an admin from the transaction monitor; it
    # flags a row for review and never changes money movement or status. ---
    aml_flagged: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    sender: Mapped["User"] = relationship("User", foreign_keys=[sender_id])
    beneficiary: Mapped["Beneficiary"] = relationship("Beneficiary", foreign_keys=[beneficiary_id])
    recipient: Mapped[Optional["User"]] = relationship("User", foreign_keys=[recipient_user_id])

    @property
    def status_label(self) -> str:
        """One overall status for history screens (FR-CI-04, FR-MQ-05)."""
        if self.cashin_status == CashInStatus.failed:
            return "Failed"
        if self.cashin_status == CashInStatus.pending:
            return "Awaiting payment"
        return {
            SettlementStatus.not_queued: "Payment received",
            SettlementStatus.queued: "Queued",
            SettlementStatus.processing: "Sending",
            SettlementStatus.completed: "Completed",
            SettlementStatus.failed: "Failed",
        }[self.settlement_status]

    @property
    def status_badge(self) -> str:
        return {
            "Failed": "danger",
            "Awaiting payment": "warning",
            "Completed": "success",
        }.get(self.status_label, "info")

    @property
    def failure_reason(self) -> Optional[str]:
        if self.cashin_status == CashInStatus.failed:
            return self.cashin_failure_reason or "Card payment failed."
        if self.settlement_status == SettlementStatus.failed:
            return self.xrpl_error_reason
        return None
