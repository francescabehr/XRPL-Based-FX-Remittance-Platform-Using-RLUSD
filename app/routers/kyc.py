from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.models.kyc import KYCSubmissionStatus
from app.models.user import KYCStatus
from app.services.kyc_service import get_active_kyc, submit_kyc

router = APIRouter(prefix="/kyc")
templates = Jinja2Templates(directory="frontend/templates")

NATIONALITIES = [
    "South African", "Zimbabwean", "Mozambican", "Zambian", "Namibian",
    "Botswanan", "Malawian", "Tanzanian", "Kenyan", "Nigerian",
    "Ghanaian", "American", "British", "Other",
]


@router.get("", response_class=HTMLResponse)
async def kyc_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/kyc", status_code=302)

    kyc = await get_active_kyc(db, user.id)

    return templates.TemplateResponse(
        "sender/kyc_form.html",
        {
            "request": request,
            "user": user,
            "kyc": kyc,
            "nationalities": NATIONALITIES,
            "flash": get_flash(request),
        },
    )


@router.post("")
async def kyc_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    full_name: str = Form(...),
    date_of_birth: date = Form(...),
    nationality: str = Form(...),
    id_number: str = Form(...),
    residential_address: str = Form(...),
    mobile: str = Form(...),
    email: str = Form(...),
    source_of_funds: str = Form(...),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # Block resubmission while pending or already approved
    if user.kyc_status == KYCStatus.approved:
        set_flash(request, "Your KYC is already approved.", "info")
        return RedirectResponse(url="/dashboard", status_code=302)
    if user.kyc_status == KYCStatus.pending:
        set_flash(request, "Your KYC submission is already under review.", "warning")
        return RedirectResponse(url="/kyc", status_code=302)

    try:
        await submit_kyc(
            db,
            user,
            full_name=full_name,
            date_of_birth=date_of_birth,
            nationality=nationality,
            id_number=id_number,
            residential_address=residential_address,
            mobile=mobile,
            email=email,
            source_of_funds=source_of_funds,
        )
    except ValueError as exc:
        kyc = await get_active_kyc(db, user.id)
        return templates.TemplateResponse(
            "sender/kyc_form.html",
            {
                "request": request,
                "user": user,
                "kyc": kyc,
                "nationalities": NATIONALITIES,
                "error": str(exc),
                "prefill": {
                    "full_name": full_name,
                    "date_of_birth": date_of_birth,
                    "nationality": nationality,
                    "id_number": id_number,
                    "residential_address": residential_address,
                    "mobile": mobile,
                    "email": email,
                    "source_of_funds": source_of_funds,
                },
            },
            status_code=400,
        )

    set_flash(request, "KYC submitted successfully. An admin will review it shortly.", "success")
    return RedirectResponse(url="/dashboard", status_code=302)
