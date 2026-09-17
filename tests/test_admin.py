from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user
from app.main import app
from app.services.auth_service import create_user


async def test_non_admin_gets_403_on_admin_route(client: AsyncClient, db: AsyncSession):
    user = await create_user(
        db,
        full_name="Not An Admin",
        email="notadmin@example.com",
        mobile="+27900000099",
        password="TestPass1!",
    )

    async def _override():
        return user

    app.dependency_overrides[get_current_user] = _override
    try:
        response = await client.get("/admin/kyc", follow_redirects=False)
        assert response.status_code == 403
    finally:
        del app.dependency_overrides[get_current_user]


async def test_unauthenticated_redirected_from_admin(client: AsyncClient):
    async def _override():
        return None

    app.dependency_overrides[get_current_user] = _override
    try:
        response = await client.get("/admin/kyc", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["location"]
    finally:
        del app.dependency_overrides[get_current_user]
