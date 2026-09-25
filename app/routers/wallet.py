"""FR-WAL-05  Recipient wallet: UCTUSD balance and incoming transfers with XRPL hashes."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash
from app.services import cashin_service, xrpl_service
from app.templating import make_templates

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


@router.get("/wallet", response_class=HTMLResponse)
async def wallet_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin", status_code=302)

    wallet = await xrpl_service.get_wallet_for_user(db, user.id)

    # The DB balance is a cache; show the ledger's figure beside it when reachable.
    ledger_balance = None
    if wallet is not None:
        try:
            ledger_balance = await asyncio.wait_for(
                xrpl_service.get_uctusd_balance(wallet.xrpl_address), timeout=5
            )
        except Exception as exc:  # noqa: BLE001 — the page must render without Testnet
            logger.warning("Ledger balance unavailable for %s: %s", wallet.xrpl_address, type(exc).__name__)

    return templates.TemplateResponse(
        "recipient/wallet.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "wallet": wallet,
            "ledger_balance": ledger_balance,
            "incoming": await cashin_service.list_incoming(db, user.id),
        },
    )
