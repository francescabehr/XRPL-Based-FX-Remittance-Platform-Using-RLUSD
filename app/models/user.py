from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Enum as SAEnum, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class KYCStatus(str, enum.Enum):
    not_submitted = "not_submitted"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    mobile: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    can_send: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    can_receive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    kyc_status: Mapped[KYCStatus] = mapped_column(
        SAEnum(KYCStatus, name="kycstatus"),
        default=KYCStatus.not_submitted,
        nullable=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    kyc_submissions: Mapped[list[KYCSubmission]] = relationship(
        "KYCSubmission",
        back_populates="user",
        foreign_keys="KYCSubmission.user_id",
        order_by="KYCSubmission.submitted_at.desc()",
    )

    @property
    def kyc_status_label(self) -> str:
        labels = {
            KYCStatus.not_submitted: "Not Submitted",
            KYCStatus.pending: "Pending Review",
            KYCStatus.approved: "Approved",
            KYCStatus.rejected: "Rejected",
        }
        return labels[self.kyc_status]

    @property
    def kyc_badge_class(self) -> str:
        classes = {
            KYCStatus.not_submitted: "secondary",
            KYCStatus.pending: "warning",
            KYCStatus.approved: "success",
            KYCStatus.rejected: "danger",
        }
        return classes[self.kyc_status]
