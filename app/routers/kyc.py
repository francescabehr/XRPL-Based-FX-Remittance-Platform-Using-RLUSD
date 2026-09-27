from datetime import date

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.models.kyc import KYCSubmissionStatus
from app.models.user import KYCStatus
from app.services.kyc_service import get_active_kyc, submit_kyc
from app.templating import make_templates

router = APIRouter(prefix="/kyc")
templates = make_templates()

NATIONALITIES = [
    "South African", "Zimbabwean", "Mozambican", "Zambian", "Namibian",
    "Botswanan", "Malawian", "Tanzanian", "Kenyan", "Nigerian",
    "Ghanaian", "American", "British", "Other",
]

# Source-of-funds choices for the dropdown. "Other" reveals a text box; the stored
# value is the chosen label or the typed description (free text, as before).
SOURCES_OF_FUNDS = [
    "Salary or wages", "Business income", "Savings", "Investments or dividends",
    "Pension", "Gift or family support", "Sale of property or assets",
]
OTHER_SOURCE = "Other"


def _source_prefill(choice: str, other: str = "") -> dict:
    """Which option to select and what to put in the 'Other' box when re-rendering."""
    if choice in SOURCES_OF_FUNDS:
        return {"source_of_funds_choice": choice, "source_of_funds_other": ""}
    return {"source_of_funds_choice": OTHER_SOURCE if (choice or other) else "",
            "source_of_funds_other": other if choice == OTHER_SOURCE else choice}


@router.get("", response_class=HTMLResponse)
async def kyc_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin", status_code=302)

    kyc = await get_active_kyc(db, user.id)

    return templates.TemplateResponse(
        "sender/kyc_form.html",
        {
            "request": request,
            "user": user,
            "kyc": kyc,
            "nationalities": NATIONALITIES,
            "sources_of_funds": SOURCES_OF_FUNDS,
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
    source_of_funds_other: str = Form(""),
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

    # "Other" + a description stores the description; the service is unchanged.
    choice, described = source_of_funds.strip(), source_of_funds_other.strip()
    source = described if choice == OTHER_SOURCE else choice
    if not choice:
        error = "Please choose your source of funds."
    elif choice == OTHER_SOURCE and not described:
        error = "Please describe your source of funds."
    else:
        error = None

    if error is None:
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
                source_of_funds=source,
            )
        except ValueError as exc:
            error = str(exc)

    if error is not None:
        kyc = await get_active_kyc(db, user.id)
        return templates.TemplateResponse(
            "sender/kyc_form.html",
            {
                "request": request,
                "user": user,
                "kyc": kyc,
                "nationalities": NATIONALITIES,
                "sources_of_funds": SOURCES_OF_FUNDS,
                "error": error,
                "prefill": {
                    "full_name": full_name,
                    "date_of_birth": date_of_birth,
                    "nationality": nationality,
                    "id_number": id_number,
                    "residential_address": residential_address,
                    "mobile": mobile,
                    "email": email,
                    **_source_prefill(choice, described),
                },
            },
            status_code=400,
        )

    set_flash(request, "KYC submitted successfully. An admin will review it shortly.", "success")
    return RedirectResponse(url="/dashboard", status_code=302)
