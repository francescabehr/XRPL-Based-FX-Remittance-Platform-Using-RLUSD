"""FR-CO-01..04  Recipient cash-out: request with a priced preview, then track it."""
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, set_flash
from app.services import cashout_service, xrpl_service
from app.services.cashout_service import CashOutError, CashOutPricing
from app.templating import make_templates

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()


def _require_recipient(user):
    """Cash-out belongs to recipients; admins and non-recipients are sent elsewhere."""
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/cashout", status_code=302)
    if not user.can_receive:
        return RedirectResponse(url="/dashboard", status_code=302)
    return None


async def _form_context(request: Request, db: AsyncSession, user, **extra):
    return {
        "request": request,
        "user": user,
        "flash": get_flash(request),
        "wallet": await xrpl_service.get_wallet_for_user(db, user.id),
        "available": await cashout_service.available_balance(db, user.id),
        "held": await cashout_service.held_for_requests(db, user.id),
        **extra,
    }


@router.get("/cashout", response_class=HTMLResponse)
async def cashout_form(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_recipient(user)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "recipient/cashout_form.html", await _form_context(request, db, user)
    )


@router.post("/cashout/preview", response_class=HTMLResponse)
async def cashout_preview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    uctusd_amount: str = Form(...),
    target_currency: str = Form(...),
):
    """FR-CO-02: show the payout the recipient would receive, before they commit."""
    redirect = _require_recipient(user)
    if redirect:
        return redirect

    try:
        amount = cashout_service.parse_amount(uctusd_amount)
        currency = cashout_service.parse_currency(target_currency)
        available = await cashout_service.available_balance(db, user.id)
        if amount > available:
            raise CashOutError(
                f"You can cash out at most {available:,.6f} UCTUSD right now."
            )
        pricing = await cashout_service.price_cashout(db, amount, currency)
    except CashOutError as exc:
        context = await _form_context(request, db, user)
        context["error"] = str(exc)
        context["amount_value"] = uctusd_amount
        context["currency_value"] = target_currency
        return templates.TemplateResponse("recipient/cashout_form.html", context, status_code=400)

    return templates.TemplateResponse(
        "recipient/cashout_review.html", await _form_context(request, db, user, pricing=pricing)
    )


@router.post("/cashout")
async def cashout_create(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    uctusd_amount: str = Form(...),
    target_currency: str = Form(...),
    market_rate: str = Form(...),
    cashout_fee_usd: str = Form(...),
    net_payout: str = Form(...),
):
    """FR-CO-01: create the request.

    The pricing fields are what the recipient was shown. They are compared against
    a fresh server-side computation and then discarded — the row is always written
    from the server's own figures.
    """
    redirect = _require_recipient(user)
    if redirect:
        return redirect

    try:
        amount = cashout_service.parse_amount(uctusd_amount)
        currency = cashout_service.parse_currency(target_currency)
        accepted = CashOutPricing(
            uctusd_amount=amount,
            target_currency=currency,
            market_rate=Decimal(market_rate),
            cashout_fee_percentage=Decimal("0"),  # not part of the comparison
            cashout_fee_min_usd=Decimal("0"),
            cashout_fee_usd=Decimal(cashout_fee_usd),
            net_payout=Decimal(net_payout),
        )
        req = await cashout_service.create_request(
            db, user, amount=amount, currency=currency, accepted=accepted
        )
    except cashout_service.PricingChanged as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/cashout", status_code=302)
    except (CashOutError, ArithmeticError, ValueError) as exc:
        message = str(exc) if isinstance(exc, CashOutError) else "Those cash-out details were not valid."
        set_flash(request, message, "danger")
        return RedirectResponse(url="/cashout", status_code=302)

    set_flash(
        request,
        f"Cash-out requested: {req.uctusd_amount:,.6f} UCTUSD. An administrator will review it.",
        "success",
    )
    return RedirectResponse(url="/cashout/history", status_code=302)


@router.get("/cashout/history", response_class=HTMLResponse)
async def cashout_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """FR-CO-04: real-time status of pending and past cash-outs."""
    redirect = _require_recipient(user)
    if redirect:
        return redirect
    return templates.TemplateResponse(
        "recipient/cashout_history.html",
        await _form_context(request, db, user, requests=await cashout_service.list_for_recipient(db, user.id)),
    )


@router.get("/cashout/{request_id}", response_class=HTMLResponse)
async def cashout_detail(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    redirect = _require_recipient(user)
    if redirect:
        return redirect

    # Scoped to the signed-in recipient: another user's id simply is not found.
    req = await cashout_service.get_request(db, request_id, recipient_id=user.id)
    if req is None:
        set_flash(request, "Cash-out request not found.", "danger")
        return RedirectResponse(url="/cashout/history", status_code=302)

    return templates.TemplateResponse(
        "recipient/cashout_detail.html",
        {"request": request, "user": user, "flash": get_flash(request), "req": req},
    )
