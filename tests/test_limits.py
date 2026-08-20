from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import KYCStatus
from app.services.auth_service import create_user
from app.services.kyc_service import approve_kyc, submit_kyc
from app.services.limit_service import check_limit, get_limit_tier, update_tier
from datetime import date


async def _approved_user(db, suffix):
    user = await create_user(
        db, full_name=f"User {suffix}", email=f"u{suffix}@test.com",
        mobile=f"+2782200{suffix}", password="Pass1234!",
    )
    admin = await create_user(
        db, full_name=f"Admin {suffix}", email=f"adm{suffix}@test.com",
        mobile=f"+2782300{suffix}", password="Pass1234!", is_admin=True,
    )
    sub = await submit_kyc(
        db, user,
        full_name=user.full_name, date_of_birth=date(1990, 1, 1),
        nationality="South African", id_number="9001010001087",
        residential_address="1 Test St", mobile=user.mobile,
        email=user.email, source_of_funds="Salary",
    )
    await approve_kyc(db, sub, admin)
    return user


async def _unverified_user(db, suffix):
    return await create_user(
        db, full_name=f"Unverif {suffix}", email=f"unv{suffix}@test.com",
        mobile=f"+2782400{suffix}", password="Pass1234!",
    )


async def test_unverified_tier_is_zero(db: AsyncSession, seed_tiers):
    user = await _unverified_user(db, "l01")
    tier = await get_limit_tier(db, user)
    assert tier is not None
    assert tier.daily_limit_zar == Decimal("0")
    assert tier.monthly_limit_zar == Decimal("0")


async def test_approved_gets_standard_tier(db: AsyncSession, seed_tiers):
    user = await _approved_user(db, "l02")
    tier = await get_limit_tier(db, user)
    assert tier is not None
    assert tier.tier_name == "standard"
    assert tier.daily_limit_zar == Decimal("10000")


async def test_unverified_blocked(db: AsyncSession, seed_tiers):
    user = await _unverified_user(db, "l03")
    result = await check_limit(db, user, Decimal("100"))
    assert result["allowed"] is False
    assert "KYC" in result["reason"]


async def test_approved_within_limit_allowed(db: AsyncSession, seed_tiers):
    user = await _approved_user(db, "l04")
    result = await check_limit(db, user, Decimal("5000"))
    assert result["allowed"] is True
    assert result["daily_remaining"] == Decimal("10000")


async def test_amount_over_daily_limit_blocked(db: AsyncSession, seed_tiers):
    user = await _approved_user(db, "l05")
    result = await check_limit(db, user, Decimal("15000"))
    assert result["allowed"] is False
    assert "Daily" in result["reason"]
    assert "R10,000.00" in result["reason"]


async def test_amount_over_monthly_limit_blocked(db: AsyncSession, seed_tiers):
    user = await _approved_user(db, "l06")
    # Update standard tier to have a very low monthly limit for this test
    tier = await get_limit_tier(db, user)
    await update_tier(db, tier, daily_limit_zar=Decimal("10000"), monthly_limit_zar=Decimal("200"))

    result = await check_limit(db, user, Decimal("500"))
    assert result["allowed"] is False
    assert "Monthly" in result["reason"]


async def test_admin_can_update_tier(db: AsyncSession, seed_tiers):
    user = await _approved_user(db, "l07")
    tier = await get_limit_tier(db, user)
    updated = await update_tier(db, tier, daily_limit_zar=Decimal("20000"), monthly_limit_zar=Decimal("100000"))
    assert updated.daily_limit_zar == Decimal("20000")
    # New limit takes effect immediately
    result = await check_limit(db, user, Decimal("15000"))
    assert result["allowed"] is True
