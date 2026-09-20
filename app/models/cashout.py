from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.beneficiary import PayoutCurrency


class CashOutStatus(str, enum.Enum):
    """Forward-only flow (FR-CO-03): requested -> approved -> completed | failed.

    There is deliberately no `processing` state: the worker claims a row with
    burn_started_at instead, so the ledger-facing machinery never adds a status
    the requirement does not name.
    """

    requested = "requested"
    approved = "approved"
    completed = "completed"
    failed = "failed"


class CashOutRequest(Base):
    """A recipient converting UCTUSD to fiat: burn on-ledger, simulate the payout.

    The settlement path in reverse. Pricing is snapshotted at request time and is
    never recomputed afterwards, so the terms the recipient accepted are the terms
    that are honoured at approval (FR-CO-02).

    Money moves exactly once: balance_uctusd is debited when an admin approves
    (the reserve, FR-CO-06) and restored only by the single conditional transition
    that moves the row approved -> failed. The full uctusd_amount is burned; the
    cash-out fee is taken out of the simulated fiat payout, not out of the burn.
    """

    __tablename__ = "cashout_requests"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recipient_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    # Which wallet was debited and burned from, snapshotted at request time.
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("wallets.id"), nullable=False
    )

    uctusd_amount: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    target_currency: Mapped[PayoutCurrency] = mapped_column(
        SAEnum(PayoutCurrency, name="payoutcurrency"), nullable=False
    )

    # --- pricing snapshot (FR-CO-02); approval uses these, never a recompute ---
    market_rate: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    cashout_fee_percentage: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    cashout_fee_min_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    cashout_fee_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # In target_currency: ZAR quantised to 2 dp, USD to 6 dp.
    net_payout: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)

    status: Mapped[CashOutStatus] = mapped_column(
        SAEnum(CashOutStatus, name="cashoutstatus"),
        nullable=False,
        default=CashOutStatus.requested,
        server_default=CashOutStatus.requested.value,
    )

    # --- admin action (FR-CO-05) ---
    approved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=True
    )
    approved_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # --- burn + recovery metadata ---
    # Queue dedup key: the worker claims on this before it can sign anything.
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4
    )
    # Recipient -> issuer hash, persisted after signing and BEFORE submission.
    xrpl_burn_tx_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    # Reliable-submission metadata: the ledger range in which this hash could ever
    # appear. Reconcile needs both to prove a missing transaction can never land.
    # https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission
    burn_last_ledger_sequence: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    burn_submitted_ledger_index: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Worker claim flag (not a status). NULL means no worker owns this row.
    burn_started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    burn_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    # --- simulated fiat payout (no real money movement anywhere) ---
    fiat_payout_reference: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    fiat_paid_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    recipient: Mapped["User"] = relationship("User", foreign_keys=[recipient_user_id])
    wallet: Mapped["Wallet"] = relationship("Wallet", foreign_keys=[wallet_id])
    approver: Mapped[Optional["User"]] = relationship("User", foreign_keys=[approved_by])

    # An approved row whose burn outcome could not be determined. The reserve stays
    # debited and no worker may resubmit it — only an admin reconcile resolves it.
    @property
    def awaiting_ledger_confirmation(self) -> bool:
        return (
            self.status == CashOutStatus.approved
            and self.xrpl_burn_tx_hash is not None
            and (self.failure_reason or "").startswith("outcome_unknown")
        )

    @property
    def status_label(self) -> str:
        """One overall status for the recipient's history screen (FR-CO-04)."""
        if self.awaiting_ledger_confirmation:
            return "Awaiting ledger confirmation"
        return {
            CashOutStatus.requested: "Requested",
            CashOutStatus.approved: "Approved",
            CashOutStatus.completed: "Completed",
            CashOutStatus.failed: "Failed",
        }[self.status]

    @property
    def status_badge(self) -> str:
        return {
            "Requested": "secondary",
            "Approved": "info",
            "Awaiting ledger confirmation": "warning",
            "Completed": "success",
            "Failed": "danger",
        }[self.status_label]

    def __repr__(self) -> str:
        return f"<CashOutRequest {self.id} {self.uctusd_amount} {self.status.value}>"
