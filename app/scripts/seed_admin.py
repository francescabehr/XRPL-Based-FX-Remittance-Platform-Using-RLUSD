"""Create the default admin user. Run with: make seed-admin"""
import asyncio

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import KYCStatus, User
from app.security.hashing import get_password_hash
import uuid


async def seed() -> None:
    # Normalised exactly as auth_service does. Login looks users up with
    # email.lower().strip(), so an ADMIN_EMAIL with any uppercase used to create
    # an admin who could never log in — and whose duplicate check never matched,
    # so re-running this tried to insert them again.
    email = (settings.admin_email or "").lower().strip()
    mobile = (settings.admin_mobile or "").strip()

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == email))
        if result.scalar_one_or_none():
            print(f"Admin already exists: {email}")
            return

        admin = User(
            id=uuid.uuid4(),
            email=email,
            mobile=mobile,
            full_name=settings.admin_name,
            password_hash=get_password_hash(settings.admin_password),
            is_admin=True,
            can_send=False,
            can_receive=False,
            kyc_status=KYCStatus.approved,
        )
        db.add(admin)
        await db.commit()
        print(f"Admin created: {email}")


if __name__ == "__main__":
    asyncio.run(seed())
