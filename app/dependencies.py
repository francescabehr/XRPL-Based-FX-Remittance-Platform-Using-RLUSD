import uuid
from typing import Optional

from fastapi import Depends
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
