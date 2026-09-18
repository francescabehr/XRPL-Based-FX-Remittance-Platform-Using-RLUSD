import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app import models as _models  # noqa: F401 — ensures all models register with Base.metadata

# Uses a separate test DB so migrations aren't required; tables are created fresh.
# NullPool: pytest-asyncio gives each test its own event loop, and a pooled asyncpg
# connection cannot be reused across loops.
_test_engine = create_async_engine(settings.test_database_url, echo=False, poolclass=NullPool)
_TestSession = async_sessionmaker(_test_engine, expire_on_commit=False, class_=AsyncSession)


# Sync fixture with its own loops: tests each run in a function-scoped loop, so a
# session-scoped async fixture would set up and tear down on unrelated loops.
@pytest.fixture(scope="session", autouse=True)
def create_tables():
    async def _create():
        async with _test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def _drop():
        async with _test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await _test_engine.dispose()

    asyncio.run(_create())
    yield
    asyncio.run(_drop())


@pytest_asyncio.fixture
async def db():
    async with _TestSession() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def seed_tiers(db: AsyncSession):
    """Reset default limit tiers to known values for tests that exercise the limit service.

    Tiers are committed and unique on tier_name, so they outlive a test and some tests
    mutate them — reset rather than insert, to keep tests order-independent.
    """
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.platform_config import LimitTier
    import uuid

    defaults = {
        "unverified": (Decimal("0"), Decimal("0")),
        "standard": (Decimal("10000"), Decimal("50000")),
    }

    tiers = {}
    for name, (daily, monthly) in defaults.items():
        result = await db.execute(select(LimitTier).where(LimitTier.tier_name == name))
        tier = result.scalar_one_or_none()
        if tier is None:
            tier = LimitTier(id=uuid.uuid4(), tier_name=name)
            db.add(tier)
        tier.daily_limit_zar = daily
        tier.monthly_limit_zar = monthly
        tiers[name] = tier

    await db.commit()
    return tiers


@pytest_asyncio.fixture
async def seed_fee_config(db: AsyncSession):
    """Reset fee_config to the documented placeholder defaults.

    Like seed_tiers, this is committed and outlives a test, so it resets rather
    than inserts — keeping tests order-independent when one mutates the config.
    """
    from decimal import Decimal
    from sqlalchemy import select
    from app.models.platform_config import FeeConfig
    import uuid

    result = await db.execute(select(FeeConfig).where(FeeConfig.is_active.is_(True)))
    cfg = result.scalars().first()
    if cfg is None:
        cfg = FeeConfig(id=uuid.uuid4())
        db.add(cfg)

    cfg.fixed_fee_zar = Decimal("25.00")
    cfg.percentage_fee = Decimal("0.015")
    cfg.fx_margin = Decimal("0.02")
    cfg.cashout_fee_percentage = Decimal("0.01")
    cfg.cashout_fee_min_usd = Decimal("1")
    cfg.market_rate_zar_per_usd = Decimal("18.50")
    cfg.is_active = True

    await db.commit()
    return cfg


@pytest_asyncio.fixture
async def client(db: AsyncSession):
    async def override_get_db():
        yield db

    app.dependency_overrides[get_db] = override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
