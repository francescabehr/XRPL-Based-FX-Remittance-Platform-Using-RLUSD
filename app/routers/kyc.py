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

# Source-of-funds choices for the dropdown; the template adds "Other", which needs a
# description. Stored as the chosen label, or "Other: <description>". Submissions made
# before the dropdown hold free text and still display as-is (it's just a string).
SOURCES_OF_FUNDS = [
    "Salary", "Business income", "Savings", "Investments", "Pension", "Gift or family support",
]
OTHER_SOURCE = "Other"
OTHER_MAX_LENGTH = 200


def _source_of_funds(choice: str, described: str) -> tuple[str | None, str | None]:
    """(value to store, error). Only a listed option or Other + a description is accepted."""
    if not choice:
        return None, "Please choose your source of funds."
    if choice in SOURCES_OF_FUNDS:
        return choice, None
    if choice != OTHER_SOURCE:
        return None, "Please choose a source of funds from the list."
    if not described:
        return None, "Please describe your source of funds."
    if len(described) > OTHER_MAX_LENGTH:
        return None, f"Please keep the description under {OTHER_MAX_LENGTH} characters."
    return f"{OTHER_SOURCE}: {described}", None


def _source_prefill(choice: str, other: str = "") -> dict:
    """Which option to select and what to put in the 'Other' box when re-rendering.
    An unknown (tampered) choice falls back to no selection."""
    if choice in SOURCES_OF_FUNDS or choice == OTHER_SOURCE:
        return {"source_of_funds_choice": choice,
                "source_of_funds_other": other if choice == OTHER_SOURCE else ""}
    return {"source_of_funds_choice": "", "source_of_funds_other": ""}


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

    # Validated here, against the list; the service stores whatever string it is given.
    choice, described = source_of_funds.strip(), source_of_funds_other.strip()
    source, error = _source_of_funds(choice, described)

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
