"""Create the default admin user. Run with: make seed-admin"""
import asyncio

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal
from app.models.user import KYCStatus, User
from app.security.hashing import get_password_hash
import uuid


async def seed() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == settings.admin_email))
        if result.scalar_one_or_none():
            print(f"Admin already exists: {settings.admin_email}")
            return

        admin = User(
            id=uuid.uuid4(),
            email=settings.admin_email,
            mobile=settings.admin_mobile,
            full_name=settings.admin_name,
            password_hash=get_password_hash(settings.admin_password),
            is_admin=True,
            can_send=False,
            can_receive=False,
            kyc_status=KYCStatus.approved,
        )
        db.add(admin)
        await db.commit()
        print(f"Admin created: {settings.admin_email}")


if __name__ == "__main__":
    asyncio.run(seed())
