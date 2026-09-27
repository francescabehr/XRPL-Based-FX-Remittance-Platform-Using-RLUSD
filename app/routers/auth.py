from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services.auth_service import (
    EMAIL_INVALID, MOBILE_INVALID, authenticate_user, create_user, email_is_valid, mobile_is_valid, password_problems,
)
from app.templating import make_templates

router = APIRouter()
templates = make_templates()


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request, user=Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/dashboard" if not user.is_admin else "/admin", status_code=302)
    return templates.TemplateResponse("public/landing.html", {"request": request, "flash": get_flash(request)})


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, user=Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/dashboard" if not user.is_admin else "/admin", status_code=302)
    return templates.TemplateResponse("auth/login.html", {"request": request, "flash": get_flash(request)})


@router.post("/login")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(db, email, password)
    if not user:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid email or password.", "prefill_email": email},
            status_code=401,
        )
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/admin" if user.is_admin else "/dashboard", status_code=302)


@router.get("/register", response_class=HTMLResponse)
async def register_form(request: Request, user=Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=302)
    return templates.TemplateResponse("auth/register.html", {"request": request})


@router.post("/register")
async def register(
    request: Request,
    full_name: str = Form(...),
    email: str = Form(...),
    mobile: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    # Format rules shared with profile edits (checked here, not in create_user, so
    # seed scripts are unaffected).
    errors = []
    if not email_is_valid(email.lower().strip()):
        errors.append(EMAIL_INVALID)
    if not mobile_is_valid(mobile.strip()):
        errors.append(MOBILE_INVALID)
    errors += password_problems(password, confirm_password)

    if not errors:
        try:
            user = await create_user(db, full_name=full_name, email=email, mobile=mobile, password=password)
            request.session["user_id"] = str(user.id)
            set_flash(request, "Account created. Welcome! Complete your KYC to start sending money.", "success")
            return RedirectResponse(url="/dashboard", status_code=302)
        except ValueError as exc:
            errors.append(str(exc))

    return templates.TemplateResponse(
        "auth/register.html",
        {
            "request": request,
            "errors": errors,
            "prefill": {"full_name": full_name, "email": email, "mobile": mobile},
        },
        status_code=400,
    )


@router.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
