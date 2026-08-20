from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash
from app.services.kyc_service import get_active_kyc

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

    return templates.TemplateResponse(
        "sender/dashboard.html",
        {
            "request": request,
            "user": user,
            "kyc": kyc,
            "flash": get_flash(request),
        },
    )
