import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services.beneficiary_service import (
    create_beneficiary,
    delete_beneficiary,
    get_beneficiary,
    list_beneficiaries,
    update_beneficiary,
)

router = APIRouter(prefix="/beneficiaries")
templates = Jinja2Templates(directory="frontend/templates")

COUNTRIES = [
    "South Africa", "Zimbabwe", "Mozambique", "Zambia", "Namibia",
    "Botswana", "Malawi", "Tanzania", "Kenya", "Nigeria", "Ghana",
    "Uganda", "Rwanda", "Ethiopia", "Angola", "Lesotho", "Eswatini",
    "United States", "United Kingdom", "Australia", "Canada",
    "Germany", "Netherlands", "Portugal", "Other",
]

RELATIONSHIPS = [
    "Parent", "Child", "Sibling", "Spouse / Partner",
    "Friend", "Business Associate", "Employee", "Other",
]


@router.get("", response_class=HTMLResponse)
async def beneficiary_list(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/kyc", status_code=302)

    bens = await list_beneficiaries(db, user.id)
    return templates.TemplateResponse(
        "sender/beneficiaries.html",
        {
            "request": request,
            "user": user,
            "beneficiaries": bens,
            "flash": get_flash(request),
        },
    )


@router.get("/new", response_class=HTMLResponse)
async def new_form(
    request: Request,
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(
        "sender/beneficiary_form.html",
        {
            "request": request,
            "user": user,
            "countries": COUNTRIES,
            "relationships": RELATIONSHIPS,
            "editing": False,
        },
    )


@router.post("/new")
async def create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    full_name: str = Form(...),
    email: str = Form(""),
    mobile: str = Form(""),
    country: str = Form(...),
    payout_currency: str = Form(...),
    relationship: str = Form(...),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    try:
        await create_beneficiary(
            db, user,
            full_name=full_name,
            email=email or None,
            mobile=mobile or None,
            country=country,
            payout_currency=payout_currency,
            relationship=relationship,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "sender/beneficiary_form.html",
            {
                "request": request,
                "user": user,
                "countries": COUNTRIES,
                "relationships": RELATIONSHIPS,
                "editing": False,
                "error": str(exc),
                "prefill": {
                    "full_name": full_name, "email": email, "mobile": mobile,
                    "country": country, "payout_currency": payout_currency,
                    "relationship": relationship,
                },
            },
            status_code=400,
        )

    set_flash(request, f"{full_name} added as a beneficiary.", "success")
    return RedirectResponse(url="/beneficiaries", status_code=302)


@router.get("/{beneficiary_id}/edit", response_class=HTMLResponse)
async def edit_form(
    beneficiary_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    ben = await get_beneficiary(db, beneficiary_id, user.id)
    if not ben:
        set_flash(request, "Beneficiary not found.", "danger")
        return RedirectResponse(url="/beneficiaries", status_code=302)

    return templates.TemplateResponse(
        "sender/beneficiary_form.html",
        {
            "request": request,
            "user": user,
            "countries": COUNTRIES,
            "relationships": RELATIONSHIPS,
            "editing": True,
            "ben": ben,
        },
    )


@router.post("/{beneficiary_id}/edit")
async def edit_save(
    beneficiary_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    full_name: str = Form(...),
    email: str = Form(""),
    mobile: str = Form(""),
    country: str = Form(...),
    payout_currency: str = Form(...),
    relationship: str = Form(...),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    ben = await get_beneficiary(db, beneficiary_id, user.id)
    if not ben:
        set_flash(request, "Beneficiary not found.", "danger")
        return RedirectResponse(url="/beneficiaries", status_code=302)

    try:
        await update_beneficiary(
            db, ben,
            full_name=full_name,
            email=email or None,
            mobile=mobile or None,
            country=country,
            payout_currency=payout_currency,
            relationship=relationship,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "sender/beneficiary_form.html",
            {
                "request": request,
                "user": user,
                "countries": COUNTRIES,
                "relationships": RELATIONSHIPS,
                "editing": True,
                "ben": ben,
                "error": str(exc),
            },
            status_code=400,
        )

    set_flash(request, f"{full_name} updated.", "success")
    return RedirectResponse(url="/beneficiaries", status_code=302)


@router.post("/{beneficiary_id}/delete")
async def delete(
    beneficiary_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    ben = await get_beneficiary(db, beneficiary_id, user.id)
    if ben:
        name = ben.full_name
        await delete_beneficiary(db, ben)
        set_flash(request, f"{name} removed.", "info")
    return RedirectResponse(url="/beneficiaries", status_code=302)
