import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth_service import authenticate_user, create_user, get_user_by_email


async def test_create_user(db: AsyncSession):
    user = await create_user(db, full_name="Alice Smith", email="alice@example.com", mobile="+27111111111", password="Secret123!")
    assert user.id is not None
    assert user.email == "alice@example.com"
    assert user.password_hash != "Secret123!"
    assert user.kyc_status.value == "not_submitted"


async def test_duplicate_email_rejected(db: AsyncSession):
    await create_user(db, full_name="Bob", email="bob@example.com", mobile="+27222222221", password="pass1234")
    with pytest.raises(ValueError, match="email"):
        await create_user(db, full_name="Bob2", email="bob@example.com", mobile="+27222222229", password="pass1234")


async def test_duplicate_mobile_rejected(db: AsyncSession):
    await create_user(db, full_name="Carol", email="carol@example.com", mobile="+27333333333", password="pass1234")
    with pytest.raises(ValueError, match="mobile"):
        await create_user(db, full_name="Carol2", email="carol2@example.com", mobile="+27333333333", password="pass1234")


async def test_authenticate_correct_password(db: AsyncSession):
    await create_user(db, full_name="Dave", email="dave@example.com", mobile="+27444444444", password="MyPass999!")
    user = await authenticate_user(db, "dave@example.com", "MyPass999!")
    assert user is not None
    assert user.email == "dave@example.com"


async def test_authenticate_wrong_password(db: AsyncSession):
    await create_user(db, full_name="Eve", email="eve@example.com", mobile="+27555555555", password="correct-pass")
    user = await authenticate_user(db, "eve@example.com", "wrong-pass")
    assert user is None


async def test_authenticate_unknown_email(db: AsyncSession):
    user = await authenticate_user(db, "nobody@example.com", "anything")
    assert user is None


async def test_register_endpoint(client: AsyncClient):
    response = await client.post("/register", data={
        "full_name": "Frank Test",
        "email": "frank@example.com",
        "mobile": "+27666666666",
        "password": "Secure123!",
        "confirm_password": "Secure123!",
    }, follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/dashboard"


async def test_register_password_mismatch(client: AsyncClient):
    response = await client.post("/register", data={
        "full_name": "Grace",
        "email": "grace@example.com",
        "mobile": "+27777777777",
        "password": "Secure123!",
        "confirm_password": "Different!",
    })
    assert response.status_code == 400
    assert b"do not match" in response.content


async def test_login_invalid_credentials(client: AsyncClient):
    response = await client.post("/login", data={"email": "noone@example.com", "password": "wrong"})
    assert response.status_code == 401
    assert b"Invalid" in response.content
