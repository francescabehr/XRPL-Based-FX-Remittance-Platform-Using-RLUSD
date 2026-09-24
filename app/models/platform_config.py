from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class LimitTier(Base):
    """Configurable daily/monthly remittance limits per KYC tier (FR-LIM-01..05)."""

    __tablename__ = "limit_tiers"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tier_name: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    daily_limit_zar: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    monthly_limit_zar: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)


class FeeConfig(Base):
    """Platform fee & FX margin configuration (FR-FX-08, FR-ADM-06).

    Exactly one row should be active at a time. Quotes snapshot the values they
    used onto the transaction row, so editing this table changes new quotes only.
    """

    __tablename__ = "fee_config"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # ZAR flat fee charged per remittance.
    fixed_fee_zar: Mapped[Decimal] = mapped_column(Numeric(20, 2), nullable=False)
    # Fraction of the ZAR send amount, e.g. 0.015 == 1.5%.
    percentage_fee: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    # Fraction added to the market rate, e.g. 0.02 == 2%.
    fx_margin: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    # Fraction of the UCTUSD amount taken at cash-out, e.g. 0.01 == 1%.
    cashout_fee_percentage: Mapped[Decimal] = mapped_column(Numeric(10, 6), nullable=False)
    # Floor on the cash-out fee, in USD.
    cashout_fee_min_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    # Smallest remittance the platform will quote or accept, in ZAR. Below this the
    # fee consumes the send and the quote yields a zero/negative UCTUSD amount.
    min_send_zar: Mapped[Decimal] = mapped_column(
        Numeric(20, 2), nullable=False, server_default="50.00"
    )
    # Mock market rate (ZAR per 1 USD) used while RATE_SOURCE is "config".
    market_rate_zar_per_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
