from decimal import Decimal

from typing import Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services import auth_service, cashout_service, xrpl_service
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


async def _profile_page(request: Request, db: AsyncSession, user, *, flash=None):
    """Read-only account summary with Edit details / Change password buttons. Only the
    wallet's public address is rendered — never its encrypted key or key id."""
    context = {"request": request, "user": user, "flash": flash}
    if not user.is_admin:
        if user.can_send:
            context.update(await _limit_context(db, user))
        if user.can_receive:
            context["wallet"] = await xrpl_service.get_wallet_for_user(db, user.id)
    return templates.TemplateResponse("account/profile.html", context)


async def _edit_page(request: Request, db: AsyncSession, user, *, flash=None, details=None, status_code=200):
    """The details form. `details` re-fills it after an error."""
    return templates.TemplateResponse("account/profile_edit.html", {
        "request": request, "user": user, "flash": flash,
        "name_editable": await auth_service.name_is_editable(db, user),
        "details": details or {"full_name": user.full_name, "email": user.email, "mobile": user.mobile},
    }, status_code=status_code)


def _password_page(request: Request, user, *, flash=None, status_code=200):
    """The password form. Its inputs are never re-filled."""
    return templates.TemplateResponse("account/profile_password.html",
                                      {"request": request, "user": user, "flash": flash}, status_code=status_code)


@router.get("/profile", response_class=HTMLResponse)
async def profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return await _profile_page(request, db, user, flash=get_flash(request))


@router.get("/profile/edit", response_class=HTMLResponse)
async def profile_edit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return await _edit_page(request, db, user, flash=get_flash(request))


@router.post("/profile")
async def profile_update(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    email: str = Form(""),        # blanks fail validation with a flash, not a 422
    mobile: str = Form(""),
    full_name: Optional[str] = Form(None),
):
    """Email, mobile and (before any KYC submission) name. The service enforces the name lock."""
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    try:
        changed = await auth_service.update_profile(db, user, email=email, mobile=mobile, full_name=full_name)
    except auth_service.ProfileError as exc:
        details = {"full_name": full_name if full_name is not None else user.full_name, "email": email, "mobile": mobile}
        return await _edit_page(request, db, user, flash={"message": str(exc), "kind": "danger"},
                                details=details, status_code=400)
    set_flash(request, "Your details have been updated." if changed else "No changes to save.",
              "success" if changed else "info")
    return RedirectResponse(url="/profile", status_code=302)


@router.get("/profile/password", response_class=HTMLResponse)
async def profile_password_form(request: Request, user=Depends(get_current_user)):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return _password_page(request, user, flash=get_flash(request))


@router.post("/profile/password")
async def profile_password(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    try:
        await auth_service.change_password(db, user, current=current_password, new=new_password,
                                           confirm=confirm_password)
    except auth_service.ProfileError as exc:
        return _password_page(request, user, flash={"message": str(exc), "kind": "danger"}, status_code=400)
    set_flash(request, "Your password has been changed.", "success")
    return RedirectResponse(url="/profile", status_code=302)
