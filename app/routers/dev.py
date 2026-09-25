"""Developer-only pages. main.py registers this router only when DEBUG=true.

The styleguide renders every design-system component from literal sample data:
no database, no settings, no user — so nothing secret can ever reach it.
"""
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.templating import make_templates

router = APIRouter(prefix="/dev")
templates = make_templates()

# A made-up hash in the XRPL format (64 hex chars). Not a real transaction.
SAMPLE_HASH = "9C1B0E5D7A34F2C8B6E1D09A4F7C3E2B5A8D6F10C4E92B7A3D5F8E6C1B0A9D2E"

SAMPLE_QUOTE = {
    "zar_amount": Decimal("1000.00"),
    "transaction_fee": Decimal("25.00"),
    "net_zar_converted": Decimal("975.00"),
    "market_rate": Decimal("18.5000"),
    "fx_margin": Decimal("0.02"),
    "exchange_rate": Decimal("18.8700"),
    "uctusd_amount": Decimal("51.669316"),
    "cashout_fee_estimate": Decimal("1.000000"),
    "payout_estimate": Decimal("50.67"),
}


@router.get("/styleguide", response_class=HTMLResponse)
async def styleguide(request: Request):
    return templates.TemplateResponse(
        "dev/styleguide.html",
        {
            "request": request,
            "sample_hash": SAMPLE_HASH,
            "quote": SAMPLE_QUOTE,
        },
    )
