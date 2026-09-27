import re
import uuid

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import KYCStatus, User
from app.security.hashing import get_password_hash, verify_password
from app.services.kyc_service import get_active_kyc

MIN_PASSWORD_LENGTH = 8
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
MOBILE_PATTERN = re.compile(r"^\+?[0-9]{7,15}$")

EMAIL_INVALID = "Enter a valid email address, like name@example.com."
MOBILE_INVALID = "Enter a valid mobile number: 7 to 15 digits, optionally starting with +, like +27821234567."
NAME_LOCKED = "Your name can't be changed after you've submitted identity verification."


class ProfileError(ValueError):
    """A profile edit was refused; the message is safe to show the user."""


def password_problems(password: str, confirm: str) -> list[str]:
    """The registration password rules, shared with the profile password change."""
    errors = []
    if password != confirm:
        errors.append("Passwords do not match.")
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.")
    return errors


def email_is_valid(email: str) -> bool:
    """For an address already lower-cased and stripped."""
    return len(email) <= 255 and EMAIL_PATTERN.match(email) is not None


def mobile_is_valid(mobile: str) -> bool:
    return MOBILE_PATTERN.match(mobile) is not None


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


async def name_is_editable(db: AsyncSession, user: User) -> bool:
    """A name can change only before any KYC submission exists: once submitted, the
    submission is the audit record of the verified name. Admins' names are fixed."""
    return not user.is_admin and await get_active_kyc(db, user.id) is None


async def _taken(db: AsyncSession, column, value: str, user_id: uuid.UUID) -> bool:
    result = await db.execute(select(User.id).where(column == value, User.id != user_id))
    return result.first() is not None


async def update_profile(
    db: AsyncSession, user: User, *, email: str, mobile: str, full_name: str | None = None,
) -> bool:
    """Change the user's email, mobile and (while allowed) name. Returns whether anything
    changed. Never touches KYC submissions. The session is keyed by user id, so a new
    email doesn't sign the user out; it is simply the login from now on."""
    email = email.lower().strip()
    mobile = mobile.strip()
    # Only a changed value is format-checked: accounts made before registration checked
    # formats may hold an older-style value and must still be able to edit the other field.
    if email != user.email and not email_is_valid(email):
        raise ProfileError(EMAIL_INVALID)
    if mobile != user.mobile and not mobile_is_valid(mobile):
        raise ProfileError(MOBILE_INVALID)

    name = user.full_name
    if full_name is not None and full_name.strip() != user.full_name:
        if not await name_is_editable(db, user):
            raise ProfileError(NAME_LOCKED)
        name = full_name.strip()
        if not name or len(name) > 255:
            raise ProfileError("Enter your full name.")

    if email != user.email and await _taken(db, User.email, email, user.id):
        raise ProfileError("Another account already uses this email address.")
    if mobile != user.mobile and await _taken(db, User.mobile, mobile, user.id):
        raise ProfileError("Another account already uses this mobile number.")

    if (email, mobile, name) == (user.email, user.mobile, user.full_name):
        return False
    user.email, user.mobile, user.full_name = email, mobile, name
    try:
        await db.commit()
    except IntegrityError:  # lost a race with another account for the same email/mobile
        await db.rollback()
        await db.refresh(user)
        raise ProfileError("That email address or mobile number is already used by another account.")
    return True


async def change_password(db: AsyncSession, user: User, *, current: str, new: str, confirm: str) -> None:
    if not verify_password(current, user.password_hash):
        raise ProfileError("Your current password is incorrect.")
    problems = password_problems(new, confirm)
    if problems:
        raise ProfileError(" ".join(problems))
    user.password_hash = get_password_hash(new)
    await db.commit()
