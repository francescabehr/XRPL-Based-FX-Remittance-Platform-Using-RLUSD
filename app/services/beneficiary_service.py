import uuid
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beneficiary import Beneficiary, PayoutCurrency
from app.models.user import User


async def list_beneficiaries(db: AsyncSession, sender_id: uuid.UUID) -> list[Beneficiary]:
    result = await db.execute(
        select(Beneficiary)
        .where(Beneficiary.sender_id == sender_id, Beneficiary.is_active == True)
        .order_by(Beneficiary.created_at.desc())
    )
    return list(result.scalars().all())


async def get_beneficiary(
    db: AsyncSession, beneficiary_id: uuid.UUID, sender_id: uuid.UUID
) -> Optional[Beneficiary]:
    result = await db.execute(
        select(Beneficiary).where(
            Beneficiary.id == beneficiary_id,
            Beneficiary.sender_id == sender_id,
            Beneficiary.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def _resolve_recipient(
    db: AsyncSession, email: Optional[str], mobile: Optional[str], sender_id: uuid.UUID
) -> Optional[uuid.UUID]:
    """FR-BEN-04: link to an existing platform user by email then mobile."""
    if email:
        result = await db.execute(
            select(User).where(User.email == email.lower().strip())
        )
        match = result.scalar_one_or_none()
        if match and match.id != sender_id:
            return match.id
    if mobile:
        result = await db.execute(
            select(User).where(User.mobile == mobile.strip())
        )
        match = result.scalar_one_or_none()
        if match and match.id != sender_id:
            return match.id
    return None


async def create_beneficiary(
    db: AsyncSession,
    sender: User,
    *,
    full_name: str,
    email: Optional[str],
    mobile: Optional[str],
    country: str,
    payout_currency: str,
    relationship: str,
) -> Beneficiary:
    if not email and not mobile:
        raise ValueError("At least one of email or mobile is required.")

    recipient_user_id = await _resolve_recipient(db, email, mobile, sender.id)

    ben = Beneficiary(
        id=uuid.uuid4(),
        sender_id=sender.id,
        recipient_user_id=recipient_user_id,
        full_name=full_name.strip(),
        email=email.lower().strip() if email else None,
        mobile=mobile.strip() if mobile else None,
        country=country,
        payout_currency=PayoutCurrency(payout_currency),
        relationship=relationship,
        is_active=True,
    )
    db.add(ben)
    await db.commit()
    await db.refresh(ben)
    return ben


async def update_beneficiary(
    db: AsyncSession,
    ben: Beneficiary,
    *,
    full_name: str,
    email: Optional[str],
    mobile: Optional[str],
    country: str,
    payout_currency: str,
    relationship: str,
) -> Beneficiary:
    if not email and not mobile:
        raise ValueError("At least one of email or mobile is required.")

    ben.full_name = full_name.strip()
    ben.email = email.lower().strip() if email else None
    ben.mobile = mobile.strip() if mobile else None
    ben.country = country
    ben.payout_currency = PayoutCurrency(payout_currency)
    ben.relationship = relationship
    ben.recipient_user_id = await _resolve_recipient(db, email, mobile, ben.sender_id)

    db.add(ben)
    await db.commit()
    await db.refresh(ben)
    return ben


async def delete_beneficiary(db: AsyncSession, ben: Beneficiary) -> None:
    ben.is_active = False
    db.add(ben)
    await db.commit()
