import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import KYCStatus, User
from app.security.hashing import get_password_hash, verify_password


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(User.email == email.lower().strip()))
    return result.scalar_one_or_none()


async def get_user_by_id(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def create_user(
    db: AsyncSession,
    *,
    full_name: str,
    email: str,
    mobile: str,
    password: str,
    is_admin: bool = False,
) -> User:
    email = email.lower().strip()
    mobile = mobile.strip()

    if await get_user_by_email(db, email):
        raise ValueError("An account with this email already exists.")

    result = await db.execute(select(User).where(User.mobile == mobile))
    if result.scalar_one_or_none():
        raise ValueError("An account with this mobile number already exists.")

    user = User(
        id=uuid.uuid4(),
        email=email,
        mobile=mobile,
        full_name=full_name.strip(),
        password_hash=get_password_hash(password),
        is_admin=is_admin,
        can_send=not is_admin,
        can_receive=False,
        kyc_status=KYCStatus.approved if is_admin else KYCStatus.not_submitted,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User | None:
    user = await get_user_by_email(db, email)
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
