import uuid
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.kyc import KYCSubmission, KYCSubmissionStatus
from app.models.user import KYCStatus, User


async def get_active_kyc(db: AsyncSession, user_id: uuid.UUID) -> Optional[KYCSubmission]:
    result = await db.execute(
        select(KYCSubmission)
        .where(KYCSubmission.user_id == user_id)
        .order_by(KYCSubmission.submitted_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_pending_submissions(db: AsyncSession) -> list[KYCSubmission]:
    result = await db.execute(
        select(KYCSubmission)
        .options(selectinload(KYCSubmission.user))
        .where(KYCSubmission.status == KYCSubmissionStatus.pending)
        .order_by(KYCSubmission.submitted_at.asc())
    )
    return list(result.scalars().all())


async def get_submission_by_id(
    db: AsyncSession, submission_id: uuid.UUID
) -> Optional[KYCSubmission]:
    result = await db.execute(
        select(KYCSubmission)
        .options(selectinload(KYCSubmission.user), selectinload(KYCSubmission.reviewer))
        .where(KYCSubmission.id == submission_id)
    )
    return result.scalar_one_or_none()


async def submit_kyc(
    db: AsyncSession,
    user: User,
    *,
    full_name: str,
    date_of_birth: date,
    nationality: str,
    id_number: str,
    residential_address: str,
    mobile: str,
    email: str,
    source_of_funds: str,
) -> KYCSubmission:
    today = date.today()
    age = (
        today.year
        - date_of_birth.year
        - ((today.month, today.day) < (date_of_birth.month, date_of_birth.day))
    )
    if age < 18:
        raise ValueError("You must be at least 18 years old to submit KYC.")
    if len(id_number.strip()) < 6:
        raise ValueError("ID number must be at least 6 characters.")

    submission = KYCSubmission(
        id=uuid.uuid4(),
        user_id=user.id,
        full_name=full_name.strip(),
        date_of_birth=date_of_birth,
        nationality=nationality,
        id_number=id_number.strip(),
        residential_address=residential_address.strip(),
        mobile=mobile.strip(),
        email=email.lower().strip(),
        source_of_funds=source_of_funds.strip(),
        status=KYCSubmissionStatus.pending,
    )
    db.add(submission)

    user.kyc_status = KYCStatus.pending
    db.add(user)

    await db.commit()
    await db.refresh(submission)
    return submission


async def approve_kyc(
    db: AsyncSession, submission: KYCSubmission, admin: User
) -> KYCSubmission:
    submission.status = KYCSubmissionStatus.approved
    submission.reviewed_by = admin.id
    submission.reviewed_at = datetime.now(timezone.utc)
    submission.rejection_reason = None
    db.add(submission)

    result = await db.execute(select(User).where(User.id == submission.user_id))
    user = result.scalar_one()
    user.kyc_status = KYCStatus.approved
    db.add(user)

    await db.commit()
    await db.refresh(submission)
    return submission


async def reject_kyc(
    db: AsyncSession, submission: KYCSubmission, admin: User, reason: str
) -> KYCSubmission:
    if not reason.strip():
        raise ValueError("A rejection reason is required.")

    submission.status = KYCSubmissionStatus.rejected
    submission.reviewed_by = admin.id
    submission.reviewed_at = datetime.now(timezone.utc)
    submission.rejection_reason = reason.strip()
    db.add(submission)

    result = await db.execute(select(User).where(User.id == submission.user_id))
    user = result.scalar_one()
    user.kyc_status = KYCStatus.rejected
    db.add(user)

    await db.commit()
    await db.refresh(submission)
    return submission
