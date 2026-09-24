import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.services import cashout_service
from app.routers import admin, auth, beneficiaries, cashout, kyc, sender, transactions, wallet


logger = logging.getLogger(__name__)


# How often the cash-out sweep runs after startup. A row only becomes sweepable
# once its claim is STUCK_AFTER (10 min) old, so this need not be tight.
SWEEP_INTERVAL_SECONDS = 120


async def _sweep_cashouts(when: str) -> None:
    """Re-publish cash-out burns that have no live message (FR-CO-05).

    Covers both the commit-before-publish window and a worker that claimed a row
    and then died before signing — RQ abandons that job rather than requeueing it,
    so nothing else would ever move the row, which sits approved and debited.
    Idempotent: the worker's claim decides, so a duplicate message is a no-op.
    """
    try:
        async with AsyncSessionLocal() as db:
            revived = await cashout_service.sweep_unpublished(db)
        if revived:
            logger.warning("%s sweep re-published %s cash-out burn(s)", when, len(revived))
    except Exception as exc:  # noqa: BLE001 — never block startup or kill the loop
        logger.error("%s cash-out sweep skipped: %s", when, type(exc).__name__)


async def _sweep_loop() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
        await _sweep_cashouts("Periodic")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _sweep_cashouts("Startup")
    # A single boot-time sweep cannot catch a worker that dies later, so keep
    # sweeping for as long as the app runs.
    sweeper = asyncio.create_task(_sweep_loop())
    try:
        yield
    finally:
        sweeper.cancel()
        try:
            await sweeper
        except asyncio.CancelledError:
            pass
        await engine.dispose()


app = FastAPI(title="XRPL Remittance Platform", lifespan=lifespan)

app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, max_age=3600 * 8)

app.mount("/static", StaticFiles(directory="frontend/static"), name="static")

app.include_router(auth.router)
app.include_router(sender.router)
app.include_router(kyc.router)
app.include_router(beneficiaries.router)
app.include_router(transactions.router)
app.include_router(wallet.router)
app.include_router(cashout.router)
app.include_router(admin.router)


@app.get("/")
async def root(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        return RedirectResponse(url="/login", status_code=302)
    return RedirectResponse(url="/dashboard", status_code=302)
