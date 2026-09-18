from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import DateTime, Enum as SAEnum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
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

    # Treasury -> recipient settlement hash; filled by the worker in Phase 6 (FR-WAL-05).
    xrpl_tx_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

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
