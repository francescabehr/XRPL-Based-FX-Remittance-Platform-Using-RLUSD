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
