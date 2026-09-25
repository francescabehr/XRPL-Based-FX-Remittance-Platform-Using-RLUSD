"""
FR-LIM-01..05  Remittance limit enforcement.

Usage is summed over real `transactions` rows for the calendar day / month as the
user experiences them — boundaries are computed in DISPLAY_TIMEZONE and converted
to UTC for the query. Timestamps are still stored in UTC; only the window edges
are local. A send at 00:30 SAST otherwise showed as "today" while counting
against the previous UTC day, so the daily allowance appeared to reset at 02:00.

Callers must pass the *same* AsyncSession they will insert the new transaction on,
so the check and the insert commit as one transaction (prevents a TOCTOU race
under concurrent sends from the same user).
"""
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Optional
from zoneinfo import ZoneInfo
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.platform_config import LimitTier
from app.models.transaction import CashInStatus, Transaction
from app.models.user import KYCStatus, User

_TIER_FOR_STATUS: dict[KYCStatus, str] = {
    KYCStatus.not_submitted: "unverified",
    KYCStatus.pending: "unverified",
    KYCStatus.rejected: "unverified",
    KYCStatus.approved: "standard",
}


async def get_limit_tier(db: AsyncSession, user: User) -> Optional[LimitTier]:
    tier_name = _TIER_FOR_STATUS.get(user.kyc_status, "unverified")
    result = await db.execute(select(LimitTier).where(LimitTier.tier_name == tier_name))
    return result.scalar_one_or_none()


async def get_all_tiers(db: AsyncSession) -> list[LimitTier]:
    result = await db.execute(select(LimitTier).order_by(LimitTier.tier_name))
    return list(result.scalars().all())


async def update_tier(
    db: AsyncSession,
    tier: LimitTier,
    daily_limit_zar: Decimal,
    monthly_limit_zar: Decimal,
) -> LimitTier:
    tier.daily_limit_zar = daily_limit_zar
    tier.monthly_limit_zar = monthly_limit_zar
    db.add(tier)
    await db.commit()
    await db.refresh(tier)
    return tier


async def get_tier_by_id(db: AsyncSession, tier_id: uuid.UUID) -> Optional[LimitTier]:
    result = await db.execute(select(LimitTier).where(LimitTier.id == tier_id))
    return result.scalar_one_or_none()


# --- usage ---

async def _sum_zar_since(
    db: AsyncSession, user_id: uuid.UUID, since: datetime
) -> Decimal:
    """Sum ZAR sent by a user since `since`.

    A failed cash-in never moved money, so it does not consume allowance.
    Everything else counts, including in-flight transactions, so that concurrent
    sends cannot each see the same headroom.
    """
    result = await db.execute(
        select(func.coalesce(func.sum(Transaction.zar_amount), 0)).where(
            Transaction.sender_id == user_id,
            Transaction.created_at >= since,
            Transaction.cashin_status != CashInStatus.failed,
        )
    )
    return Decimal(result.scalar_one())


def display_tz() -> ZoneInfo:
    """The timezone the user reads every timestamp in (templating.localtime)."""
    return ZoneInfo(settings.display_timezone)


def display_today() -> date:
    """Today's date as the user sees it, not as UTC sees it."""
    return datetime.now(display_tz()).date()


def day_start_utc(day: date) -> datetime:
    """The UTC instant at which `day` begins in DISPLAY_TIMEZONE.

    Built from the date rather than by replacing fields on an aware datetime, so
    a zone with DST cannot land on a non-existent local midnight.
    """
    return datetime.combine(day, time.min, tzinfo=display_tz()).astimezone(timezone.utc)


async def get_daily_usage(db: AsyncSession, user_id: uuid.UUID) -> Decimal:
    """ZAR sent by this user so far in the current local calendar day (FR-LIM-01)."""
    return await _sum_zar_since(db, user_id, day_start_utc(display_today()))


async def get_monthly_usage(db: AsyncSession, user_id: uuid.UUID) -> Decimal:
    """ZAR sent by this user so far in the current local calendar month (FR-LIM-02)."""
    return await _sum_zar_since(db, user_id, day_start_utc(display_today().replace(day=1)))


# --- main check ---

async def check_limit(
    db: AsyncSession, user: User, amount_zar: Decimal
) -> dict:
    """
    Returns a dict with keys:
      allowed (bool), reason (str|None),
      daily_remaining (Decimal), monthly_remaining (Decimal).
    FR-LIM-03: reason includes the exact ZAR amount still available.
    FR-LIM-04: unverified users get tier with R0 limits.
    """
    tier = await get_limit_tier(db, user)
    if tier is None:
        return {
            "allowed": False,
            "reason": "No remittance tier is configured for your account. Contact support.",
            "daily_remaining": Decimal("0"),
            "monthly_remaining": Decimal("0"),
        }

    if tier.daily_limit_zar == 0 and tier.monthly_limit_zar == 0:
        return {
            "allowed": False,
            "reason": "Your account must be KYC-approved before sending money.",
            "daily_remaining": Decimal("0"),
            "monthly_remaining": Decimal("0"),
        }

    daily_used = await get_daily_usage(db, user.id)
    monthly_used = await get_monthly_usage(db, user.id)
    daily_remaining = max(tier.daily_limit_zar - daily_used, Decimal("0"))
    monthly_remaining = max(tier.monthly_limit_zar - monthly_used, Decimal("0"))

    if daily_used + amount_zar > tier.daily_limit_zar:
        return {
            "allowed": False,
            "reason": f"Daily limit exceeded. You have R{daily_remaining:,.2f} available today.",
            "daily_remaining": daily_remaining,
            "monthly_remaining": monthly_remaining,
        }

    if monthly_used + amount_zar > tier.monthly_limit_zar:
        return {
            "allowed": False,
            "reason": f"Monthly limit exceeded. You have R{monthly_remaining:,.2f} available this month.",
            "daily_remaining": daily_remaining,
            "monthly_remaining": monthly_remaining,
        }

    return {
        "allowed": True,
        "reason": None,
        "daily_remaining": daily_remaining,
        "monthly_remaining": monthly_remaining,
    }
