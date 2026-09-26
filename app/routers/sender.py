from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash
from app.services import cashout_service, xrpl_service
from app.services.beneficiary_service import list_beneficiaries
from app.services.cashin_service import list_for_sender
from app.services.kyc_service import get_active_kyc
from app.services.limit_service import get_daily_usage, get_limit_tier, get_monthly_usage
from app.templating import make_templates

router = APIRouter()
templates = make_templates()


def _pct(used: Decimal, limit: Decimal) -> int:
    if limit <= 0:
        return 0
    return min(int((used / limit) * 100), 100)


async def _limit_context(db: AsyncSession, user) -> dict:
    """The user's tier and today's/this month's usage, as the dashboard and profile show them."""
    tier = await get_limit_tier(db, user)

    daily_used = await get_daily_usage(db, user.id)
    monthly_used = await get_monthly_usage(db, user.id)

    daily_limit = tier.daily_limit_zar if tier else Decimal("0")
    monthly_limit = tier.monthly_limit_zar if tier else Decimal("0")
    daily_remaining = max(daily_limit - daily_used, Decimal("0"))
    monthly_remaining = max(monthly_limit - monthly_used, Decimal("0"))

    return {
        "tier": tier,
        "daily_used": daily_used,
        "monthly_used": monthly_used,
        "daily_limit": daily_limit,
        "monthly_limit": monthly_limit,
        "daily_remaining": daily_remaining,
        "monthly_remaining": monthly_remaining,
        "daily_pct": _pct(daily_used, daily_limit),
        "monthly_pct": _pct(monthly_used, monthly_limit),
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin", status_code=302)

    kyc = await get_active_kyc(db, user.id)
    limits = await _limit_context(db, user)

    # Recipient side of the role-aware dashboard (read-only).
    wallet = await xrpl_service.get_wallet_for_user(db, user.id) if user.can_receive else None
    available_balance = await cashout_service.available_balance(db, user.id) if wallet else None

    return templates.TemplateResponse(
        "sender/dashboard.html",
        {
            "request": request,
            "user": user,
            "kyc": kyc,
            "flash": get_flash(request),
            **limits,
            "recent": await list_for_sender(db, user.id, limit=5),
            "beneficiary_count": len(await list_beneficiaries(db, user.id)),
            "wallet": wallet,
            "available_balance": available_balance,
        },
    )


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Read-only account summary. Only the wallet's public address is rendered —
    never its encrypted key or key id."""
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    context = {"request": request, "user": user, "flash": get_flash(request)}
    if not user.is_admin:
        if user.can_send:
            context.update(await _limit_context(db, user))
        if user.can_receive:
            context["wallet"] = await xrpl_service.get_wallet_for_user(db, user.id)
    return templates.TemplateResponse("account/profile.html", context)
