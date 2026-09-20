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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Re-publish any cash-out burn whose message was lost between the approval
    # commit and the enqueue (FR-CO-05). Idempotent: the worker's claim decides.
    try:
        async with AsyncSessionLocal() as db:
            revived = await cashout_service.sweep_unpublished(db)
        if revived:
            logger.warning("Startup sweep re-published %s cash-out burn(s)", len(revived))
    except Exception as exc:  # noqa: BLE001 — never block startup on Redis/DB
        logger.error("Startup cash-out sweep skipped: %s", type(exc).__name__)
    yield
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
