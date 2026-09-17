import uuid
from typing import Optional

from fastapi import Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from app.database import get_db
from app.models.user import User
from app.services.auth_service import get_user_by_id


async def get_current_user(
    request: Request, db: AsyncSession = Depends(get_db)
) -> Optional[User]:
    user_id_str = request.session.get("user_id")
    if not user_id_str:
        return None
    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        request.session.clear()
        return None
    return await get_user_by_id(db, user_id)


def get_flash(request: Request) -> Optional[dict]:
    flash = request.session.get("flash")
    if flash:
        del request.session["flash"]
    return flash


def set_flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


async def require_admin(user: Optional[User] = Depends(get_current_user)) -> User:
    """Single enforcement point for all admin-only routes (FR-ADM-01..07)."""
    if not user:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required.")
    return user
