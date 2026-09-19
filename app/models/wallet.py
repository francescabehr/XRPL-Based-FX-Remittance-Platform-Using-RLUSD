from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Wallet(Base):
    """A recipient's dedicated, platform-managed XRPL Testnet account (FR-WAL-01).

    The treasury wallet is NOT a row here — it lives in config. The seed is stored
    only as a Fernet token (FR-WAL-03); the key is in env, not the DB.
    """

    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # One wallet per recipient.
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True
    )

    xrpl_address: Mapped[str] = mapped_column(String(35), nullable=False, unique=True)
    encrypted_private_key: Mapped[str] = mapped_column(Text, nullable=False)
    # Fingerprint of the Fernet key that sealed encrypted_private_key (for rotation).
    key_encryption_key_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trust_set_complete: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Platform-side cache; the ledger is authoritative. Updated only after validation (FR-WAL-06).
    balance_uctusd: Mapped[Decimal] = mapped_column(
        Numeric(20, 6), default=Decimal("0"), nullable=False
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    user: Mapped["User"] = relationship("User", foreign_keys=[user_id])

    def __repr__(self) -> str:
        # Deliberately omits encrypted_private_key (FR-WAL-04).
        return f"<Wallet {self.xrpl_address} user={self.user_id}>"
