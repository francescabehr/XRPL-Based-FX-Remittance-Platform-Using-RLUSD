import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services.kyc_service import (
    approve_kyc,
    get_pending_submissions,
    get_submission_by_id,
    reject_kyc,
)
from app.services.limit_service import get_all_tiers, get_tier_by_id, update_tier

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="frontend/templates")


def _require_admin(user):
    """Return user if admin, else return a RedirectResponse."""
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if not user.is_admin:
        return RedirectResponse(url="/dashboard", status_code=302)
    return None


@router.get("/kyc", response_class=HTMLResponse)
async def kyc_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    submissions = await get_pending_submissions(db)
    return templates.TemplateResponse(
        "admin/kyc_queue.html",
        {
            "request": request,
            "user": user,
            "submissions": submissions,
            "flash": get_flash(request),
        },
    )


@router.get("/kyc/{submission_id}", response_class=HTMLResponse)
async def kyc_detail(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    return templates.TemplateResponse(
        "admin/kyc_detail.html",
        {
            "request": request,
            "user": user,
            "submission": submission,
            "flash": get_flash(request),
        },
    )


@router.post("/kyc/{submission_id}/approve")
async def kyc_approve(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    await approve_kyc(db, submission, user)
    set_flash(request, f"KYC approved for {submission.user.full_name}.", "success")
    return RedirectResponse(url="/admin/kyc", status_code=302)


@router.post("/kyc/{submission_id}/reject")
async def kyc_reject(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    reason: str = Form(...),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    try:
        await reject_kyc(db, submission, user, reason)
    except ValueError as exc:
        set_flash(request, str(exc), "danger")
        return RedirectResponse(url=f"/admin/kyc/{submission_id}", status_code=302)

    set_flash(request, f"KYC rejected for {submission.user.full_name}.", "warning")
    return RedirectResponse(url="/admin/kyc", status_code=302)


# ── Fee & Limit Configuration (FR-LIM-05, FR-FX-08) ──────────────────────────

@router.get("/config", response_class=HTMLResponse)
async def config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    tiers = await get_all_tiers(db)
    return templates.TemplateResponse(
        "admin/config.html",
        {
            "request": request,
            "user": user,
            "tiers": tiers,
            "flash": get_flash(request),
        },
    )


@router.post("/config/tiers/{tier_id}")
async def update_tier_limits(
    tier_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    daily_limit_zar: str = Form(...),
    monthly_limit_zar: str = Form(...),
):
    redirect = _require_admin(user)
    if redirect:
        return redirect

    from decimal import Decimal, InvalidOperation
    try:
        daily = Decimal(daily_limit_zar)
        monthly = Decimal(monthly_limit_zar)
        if daily < 0 or monthly < 0:
            raise ValueError("Limits cannot be negative.")
    except (InvalidOperation, ValueError) as exc:
        set_flash(request, f"Invalid value: {exc}", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    tier = await get_tier_by_id(db, tier_id)
    if not tier:
        set_flash(request, "Tier not found.", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    await update_tier(db, tier, daily, monthly)
    set_flash(request, f"Limits for '{tier.tier_name}' updated.", "success")
    return RedirectResponse(url="/admin/config", status_code=302)
