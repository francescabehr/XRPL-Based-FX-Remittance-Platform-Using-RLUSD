from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services.auth_service import authenticate_user, create_user

router = APIRouter()
templates = Jinja2Templates(directory="frontend/templates")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, user=Depends(get_current_user)):
    if user:
        return RedirectResponse(url="/dashboard" if not user.is_admin else "/admin/kyc", status_code=302)
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
    return RedirectResponse(url="/admin/kyc" if user.is_admin else "/dashboard", status_code=302)


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
    errors = []
    if password != confirm_password:
        errors.append("Passwords do not match.")
    if len(password) < 8:
        errors.append("Password must be at least 8 characters.")

    if not errors:
        try:
            user = await create_user(db, full_name=full_name, email=email, mobile=mobile, password=password)
            request.session["user_id"] = str(user.id)
            set_flash(request, "Account created — welcome! Please complete your KYC to send money.", "success")
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
