"""Public info pages linked from the site footer (UI redesign 6c).

Reachable without login. They always use the signed-out layout: `user` is never
put in the template context, only `home`, the dashboard link for a visitor who
happens to be signed in. Read-only.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.dependencies import get_current_user
from app.templating import make_templates

router = APIRouter()
templates = make_templates()

PAGES = {
    "/about": "public/about.html",
    "/contact": "public/contact.html",
    "/help": "public/help.html",
}
LEGAL_PAGES = [
    ("data-security", "Data security"),
    ("information-security", "Information security"),
    ("terms", "Terms and conditions"),
    ("privacy", "Privacy policy"),
]


def _render(request: Request, template: str, user) -> HTMLResponse:
    home = ("/admin" if user.is_admin else "/dashboard") if user else None
    return templates.TemplateResponse(template, {"request": request, "home": home, "legal_pages": LEGAL_PAGES})


@router.get("/about", response_class=HTMLResponse)
@router.get("/contact", response_class=HTMLResponse)
@router.get("/help", response_class=HTMLResponse)
async def info_page(request: Request, user=Depends(get_current_user)):
    return _render(request, PAGES[request.url.path], user)


@router.get("/legal/{slug}", response_class=HTMLResponse)
async def legal_page(slug: str, request: Request, user=Depends(get_current_user)):
    if slug not in dict(LEGAL_PAGES):
        raise HTTPException(status_code=404)
    return _render(request, f"public/legal_{slug.replace('-', '_')}.html", user)
