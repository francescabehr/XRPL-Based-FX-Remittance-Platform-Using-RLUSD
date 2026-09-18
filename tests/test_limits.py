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


# --- usage now computed from real transactions (Phase 4 un-stub, FR-LIM-01..04) ---

async def _insert_txn(db, user, zar: str, cashin_status=None, days_ago: int = 0):
    """Insert a minimal remittance row for the given sender."""
    import uuid as _uuid
    from datetime import datetime, timedelta, timezone as _tz

    from app.models.beneficiary import PayoutCurrency
    from app.models.transaction import CashInStatus, Transaction
    from app.services.beneficiary_service import create_beneficiary

    ben = await create_beneficiary(
        db, user, full_name=f"Ben {_uuid.uuid4().hex[:6]}",
        email=f"{_uuid.uuid4().hex[:8]}@test.com", mobile=None,
        country="United States", payout_currency="USD", relationship="Friend",
    )

    created = datetime.now(_tz.utc) - timedelta(days=days_ago)
    txn = Transaction(
        id=_uuid.uuid4(),
        sender_id=user.id,
        beneficiary_id=ben.id,
        zar_amount=Decimal(zar),
        transaction_fee=Decimal("40.00"),
        net_zar_converted=Decimal(zar) - Decimal("40.00"),
        market_rate=Decimal("18.500000"),
        exchange_rate=Decimal("18.870000"),
        fx_margin=Decimal("0.020000"),
        uctusd_amount=Decimal("50.874404"),
        idempotency_key=_uuid.uuid4(),
        cashin_status=cashin_status or CashInStatus.pending,
        created_at=created,
    )
    db.add(txn)
    await db.commit()
    return txn


async def test_daily_usage_reflects_inserted_transactions(db: AsyncSession, seed_tiers):
    from app.services.limit_service import get_daily_usage

    user = await _approved_user(db, "l10")
    assert await get_daily_usage(db, user.id) == Decimal("0")

    await _insert_txn(db, user, "1500.00")
    await _insert_txn(db, user, "500.00")

    usage = await get_daily_usage(db, user.id)
    assert usage == Decimal("2000.00")
    assert isinstance(usage, Decimal)


async def test_monthly_usage_reflects_inserted_transactions(db: AsyncSession, seed_tiers):
    from app.services.limit_service import get_daily_usage, get_monthly_usage

    user = await _approved_user(db, "l11")
    await _insert_txn(db, user, "1000.00")
    # Earlier this month but not today — counts monthly, not daily.
    if date.today().day > 3:
        await _insert_txn(db, user, "2000.00", days_ago=2)
        assert await get_daily_usage(db, user.id) == Decimal("1000.00")
        assert await get_monthly_usage(db, user.id) == Decimal("3000.00")
    else:
        assert await get_monthly_usage(db, user.id) == Decimal("1000.00")


async def test_failed_cashin_does_not_consume_allowance(db: AsyncSession, seed_tiers):
    from app.models.transaction import CashInStatus
    from app.services.limit_service import get_daily_usage

    user = await _approved_user(db, "l12")
    await _insert_txn(db, user, "1000.00")
    await _insert_txn(db, user, "9999.00", cashin_status=CashInStatus.failed)

    assert await get_daily_usage(db, user.id) == Decimal("1000.00")


async def test_usage_is_per_user(db: AsyncSession, seed_tiers):
    from app.services.limit_service import get_daily_usage

    one = await _approved_user(db, "l13")
    two = await _approved_user(db, "l14")
    await _insert_txn(db, one, "3000.00")

    assert await get_daily_usage(db, one.id) == Decimal("3000.00")
    assert await get_daily_usage(db, two.id) == Decimal("0")


async def test_check_limit_uses_real_usage(db: AsyncSession, seed_tiers):
    """Prior sends eat into the daily allowance (FR-LIM-03)."""
    user = await _approved_user(db, "l15")
    await _insert_txn(db, user, "9000.00")

    result = await check_limit(db, user, Decimal("2000"))
    assert result["allowed"] is False
    assert result["daily_remaining"] == Decimal("1000.00")
    assert "R1,000.00" in result["reason"]

    ok = await check_limit(db, user, Decimal("500"))
    assert ok["allowed"] is True
