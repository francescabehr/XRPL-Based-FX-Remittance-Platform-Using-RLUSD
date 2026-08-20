from decimal import Decimal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash
from app.services.kyc_service import get_active_kyc
from app.services.limit_service import get_daily_usage, get_limit_tier, get_monthly_usage

router = APIRouter()
templates = Jinja2Templates(directory="frontend/templates")


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/kyc", status_code=302)

    kyc = await get_active_kyc(db, user.id)
    tier = await get_limit_tier(db, user)

    daily_used = await get_daily_usage(db, user.id)
    monthly_used = await get_monthly_usage(db, user.id)

    daily_limit = tier.daily_limit_zar if tier else Decimal("0")
    monthly_limit = tier.monthly_limit_zar if tier else Decimal("0")
    daily_remaining = max(daily_limit - daily_used, Decimal("0"))
    monthly_remaining = max(monthly_limit - monthly_used, Decimal("0"))

    def _pct(used: Decimal, limit: Decimal) -> int:
        if limit <= 0:
            return 0
        return min(int((used / limit) * 100), 100)

    return templates.TemplateResponse(
        "sender/dashboard.html",
        {
            "request": request,
            "user": user,
            "kyc": kyc,
            "flash": get_flash(request),
            "tier": tier,
            "daily_used": daily_used,
            "monthly_used": monthly_used,
            "daily_limit": daily_limit,
            "monthly_limit": monthly_limit,
            "daily_remaining": daily_remaining,
            "monthly_remaining": monthly_remaining,
            "daily_pct": _pct(daily_used, daily_limit),
            "monthly_pct": _pct(monthly_used, monthly_limit),
        },
    )
