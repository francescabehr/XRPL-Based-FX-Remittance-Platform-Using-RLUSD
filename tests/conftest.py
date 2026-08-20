import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
import app.models  # noqa: F401 — ensures all models register with Base.metadata

# Uses a separate test DB so migrations aren't required; tables are created fresh.
_test_engine = create_async_engine(settings.test_database_url, echo=False)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)


@pytest_asyncio.fixture(scope="session", autouse=True)
async def create_tables():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await _test_engine.dispose()


@pytest_asyncio.fixture
async def db():
    async with _TestSession() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def seed_tiers(db: AsyncSession):
    """Populate default limit tiers for tests that exercise the limit service."""
    from decimal import Decimal
    from app.models.platform_config import LimitTier
    import uuid

    unverified = LimitTier(
        id=uuid.uuid4(), tier_name="unverified",
        daily_limit_zar=Decimal("0"), monthly_limit_zar=Decimal("0"),
    )
    standard = LimitTier(
        id=uuid.uuid4(), tier_name="standard",
        daily_limit_zar=Decimal("10000"), monthly_limit_zar=Decimal("50000"),
    )
    db.add_all([unverified, standard])
    await db.commit()
    return {"unverified": unverified, "standard": standard}


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
