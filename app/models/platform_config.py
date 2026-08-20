from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import Numeric, String
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
