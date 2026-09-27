"""HTML error pages for browsers (UI redesign Phase 5d).

A 403 or 404 on a page load that asks for HTML renders a page in the app shell
(or the signed-out layout). Everything else — JSON APIs such as /quote and
/status, redirects, other status codes — keeps FastAPI's default response.
Read-only: the handler only looks up who is signed in.
"""
import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import User
from app.templating import make_templates

logger = logging.getLogger(__name__)
templates = make_templates()

HTML_ERRORS = (403, 404)
# JSON endpoints keep JSON errors even when a browser opens them directly.
JSON_PREFIXES = ("/quote", "/status")


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith(JSON_PREFIXES) or request.method not in ("GET", "HEAD", "POST"):
        return False
    return "text/html" in request.headers.get("accept", "")


async def _signed_in_user(request: Request) -> Optional[User]:
    """Who is signed in, resolved as a route would (dependency overrides included).
    Any failure falls back to the signed-out page rather than a second error."""
    try:
        overrides = request.app.dependency_overrides
        if get_current_user in overrides:
            return await overrides[get_current_user]()
        if not request.session.get("user_id"):
            return None
        db_gen = overrides.get(get_db, get_db)()
        db = await db_gen.__anext__()
        try:
            return await get_current_user(request, db)
        finally:
            await db_gen.aclose()
    except Exception:  # noqa: BLE001 — an error page must always render
        logger.exception("Could not resolve the user for an error page")
        return None


async def html_http_exception_handler(request: Request, exc: StarletteHTTPException):
    if exc.status_code not in HTML_ERRORS or not _wants_html(request):
        return await http_exception_handler(request, exc)

    user = await _signed_in_user(request)
    if user is None:
        home = "/"
    else:
        home = "/admin" if user.is_admin else "/dashboard"
    return templates.TemplateResponse(
        "errors/error.html",
        {"request": request, "user": user, "status_code": exc.status_code, "home": home},
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, html_http_exception_handler)
