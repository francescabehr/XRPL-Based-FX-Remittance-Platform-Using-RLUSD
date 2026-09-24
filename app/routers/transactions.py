"""
FR-FX-07  GET /quote — authenticated, KYC-approved senders only.
FR-CI-01..05  Send Money flow, simulated card cash-in, transaction history.
"""
import uuid
from decimal import Decimal, InvalidOperation
from typing import Literal, Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user, get_flash, require_admin, set_flash
from app.models.user import KYCStatus, User
from app.schemas.transaction import QuoteResponse
from app.services import cashin_service
from app.services.beneficiary_service import (
    find_recipient_user_id,
    get_beneficiary,
    list_beneficiaries,
)
from app.services.fx_service import AmountTooSmall, FXConfigError, quote_for
from app.services.limit_service import check_limit
from app.templating import make_templates

router = APIRouter()
templates = make_templates()


async def require_approved_sender(user: Optional[User] = Depends(get_current_user)) -> User:
    """Quoting requires a logged-in, KYC-approved sender (ties FR-KYC-04)."""
    if not user:
        raise HTTPException(status_code=401, detail="You must be logged in to request a quote.")
    if not user.can_send:
        raise HTTPException(status_code=403, detail="Your account is not permitted to send money.")
    if user.kyc_status != KYCStatus.approved:
        raise HTTPException(
            status_code=403,
            detail=(
                "Your KYC verification must be approved before you can request a quote. "
                f"Current status: {user.kyc_status.value}."
            ),
        )
    return user


@router.get("/quote", response_model=QuoteResponse)
async def get_quote(
    beneficiary_id: uuid.UUID = Query(..., description="Beneficiary to quote for."),
    zar_amount: Decimal = Query(..., gt=0, description="ZAR amount the sender wants to send."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_approved_sender),
) -> QuoteResponse:
    beneficiary = await get_beneficiary(db, beneficiary_id, user.id)
    if beneficiary is None:
        raise HTTPException(status_code=404, detail="Beneficiary not found.")

    try:
        quote = await quote_for(db, zar_amount, beneficiary.payout_currency)
    except AmountTooSmall as exc:
        # The amount is the problem, not the platform — 400, not 503.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (FXConfigError, InvalidOperation) as exc:
        raise HTTPException(status_code=503, detail=f"Quote unavailable: {exc}") from exc

    # FR-LIM-03: an over-limit amount is surfaced here, before any payment step.
    limit = await check_limit(db, user, quote.zar_amount)

    return QuoteResponse(
        beneficiary_id=beneficiary.id,
        beneficiary_name=beneficiary.full_name,
        payout_currency=quote.payout_currency,
        zar_amount=quote.zar_amount,
        exchange_rate=quote.exchange_rate,
        transaction_fee=quote.transaction_fee,
        fx_margin=quote.fx_margin,
        uctusd_amount=quote.uctusd_amount,
        cashout_fee_estimate=quote.cashout_fee_estimate,
        payout_estimate=quote.payout_estimate,
        market_rate=quote.market_rate,
        net_zar_converted=quote.net_zar_converted,
        limit_ok=limit["allowed"],
        limit_reason=limit["reason"],
        daily_remaining=limit["daily_remaining"],
        monthly_remaining=limit["monthly_remaining"],
    )


# ── Send Money (HTML) ─────────────────────────────────────────────────────────

def _sender_redirect(request: Request, user: Optional[User]) -> Optional[RedirectResponse]:
    """HTML counterpart of require_approved_sender: redirect with a message instead of 403."""
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/kyc", status_code=302)
    if not user.can_send or user.kyc_status != KYCStatus.approved:
        set_flash(request, "Your KYC must be approved before you can send money.", "warning")
        return RedirectResponse(url="/dashboard", status_code=302)
    return None


def _parse_decimal(raw: str) -> Optional[Decimal]:
    """A Decimal from form input, or None if it is not a number at all."""
    try:
        return Decimal(raw.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return None


def _parse_amount(raw: str) -> Optional[Decimal]:
    amount = _parse_decimal(raw)
    return amount if amount is not None and amount > 0 else None


async def _quote_context(db: AsyncSession, user: User, beneficiary_id: str, zar_amount: str) -> dict:
    """Quote + limit + recipient check shared by the review and pay steps.

    Returns {"error": msg} when the send cannot proceed, so the page can say why.
    """
    amount = _parse_amount(zar_amount)
    if amount is None:
        return {"error": "Enter an amount greater than zero."}
    try:
        ben = await get_beneficiary(db, uuid.UUID(beneficiary_id), user.id)
    except ValueError:
        ben = None
    if ben is None:
        return {"error": "Choose one of your beneficiaries."}

    try:
        quote = await quote_for(db, amount, ben.payout_currency)
    except AmountTooSmall as exc:
        return {"error": str(exc)}
    except (FXConfigError, InvalidOperation) as exc:
        return {"error": f"Quotes are unavailable right now: {exc}"}

    limit = await check_limit(db, user, quote.zar_amount)
    ctx = {"beneficiary": ben, "quote": quote, "limit": limit}
    if not limit["allowed"]:
        ctx["error"] = limit["reason"]
    elif await find_recipient_user_id(db, ben) is None:
        ctx["error"] = (
            f"{ben.full_name} does not have an account yet. Ask them to register with the "
            "email or mobile number you saved, then try again."
        )
    return ctx


def _accepted_quote(
    exchange_rate: str, transaction_fee: str, uctusd_amount: str
) -> Optional[cashin_service.AcceptedQuote]:
    """The price the sender is agreeing to, or None if any part is unreadable.

    None means refuse. The fee may legitimately be zero (an admin can configure a
    zero fixed and percentage fee); the rate and the UCTUSD amount cannot be.
    """
    rate = _parse_amount(exchange_rate)
    fee = _parse_decimal(transaction_fee)
    uctusd = _parse_amount(uctusd_amount)
    if rate is None or fee is None or fee < 0 or uctusd is None:
        return None
    return cashin_service.AcceptedQuote(
        exchange_rate=rate, transaction_fee=fee, uctusd_amount=uctusd
    )


def _back_to_send(beneficiary_id: str, zar_amount: str) -> RedirectResponse:
    query = urlencode({"beneficiary_id": beneficiary_id, "zar_amount": zar_amount})
    return RedirectResponse(url=f"/send?{query}", status_code=302)


@router.get("/send", response_class=HTMLResponse)
async def send_start(
    request: Request,
    beneficiary_id: str = "",
    zar_amount: str = "",
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if redirect := _sender_redirect(request, user):
        return redirect
    return templates.TemplateResponse(
        "sender/send_amount.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "beneficiaries": await list_beneficiaries(db, user.id),
            "beneficiary_id": beneficiary_id,
            "zar_amount": zar_amount,
            "limit": await check_limit(db, user, Decimal("0")),
        },
    )


@router.get("/send/review", response_class=HTMLResponse)
async def send_review(
    request: Request,
    beneficiary_id: str = "",
    zar_amount: str = "",
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if redirect := _sender_redirect(request, user):
        return redirect
    ctx = await _quote_context(db, user, beneficiary_id, zar_amount)
    if "quote" not in ctx:
        set_flash(request, ctx["error"], "danger")
        return _back_to_send(beneficiary_id, zar_amount)
    return templates.TemplateResponse(
        "sender/send_review.html",
        {"request": request, "user": user, "flash": get_flash(request), **ctx},
    )


def _pay_page(request: Request, user: User, ctx: dict, error: Optional[str] = None, card_name: str = ""):
    return templates.TemplateResponse(
        "sender/send_pay.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "card_error": error,
            "card_name": card_name or user.full_name,
            "declined_card": cashin_service.DECLINED_TEST_CARD,
            **ctx,
        },
    )


@router.get("/send/pay", response_class=HTMLResponse)
async def send_pay(
    request: Request,
    beneficiary_id: str = "",
    zar_amount: str = "",
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if redirect := _sender_redirect(request, user):
        return redirect
    ctx = await _quote_context(db, user, beneficiary_id, zar_amount)
    if ctx.get("error"):
        set_flash(request, ctx["error"], "danger")
        return _back_to_send(beneficiary_id, zar_amount)
    return _pay_page(request, user, ctx)


@router.post("/remittances")
async def create_remittance(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
    beneficiary_id: str = Form(...),
    zar_amount: str = Form(...),
    # The price lock (requirements.md §382). Required: a POST that omits or
    # mangles these must be refused, never silently priced at the current rate.
    exchange_rate: str = Form(...),
    transaction_fee: str = Form(...),
    uctusd_amount: str = Form(...),
    card_number: str = Form(""),
    card_expiry: str = Form(""),
    card_cvv: str = Form(""),
    card_name: str = Form(""),
):
    if redirect := _sender_redirect(request, user):
        return redirect

    amount = _parse_amount(zar_amount)
    try:
        ben_uuid = uuid.UUID(beneficiary_id)
    except ValueError:
        ben_uuid = None
    if amount is None or ben_uuid is None:
        set_flash(request, "Your send details were incomplete. Please start again.", "danger")
        return RedirectResponse(url="/send", status_code=302)

    # An unreadable price lock is a refusal, not a skipped check: without all three
    # figures there is nothing to compare the fresh quote against.
    accepted = _accepted_quote(exchange_rate, transaction_fee, uctusd_amount)
    if accepted is None:
        set_flash(
            request,
            "We could not confirm the price you were quoted. Please review a fresh quote.",
            "danger",
        )
        return _back_to_send(beneficiary_id, zar_amount)

    try:
        txn = await cashin_service.create_remittance(
            db,
            user,
            beneficiary_id=ben_uuid,
            zar_amount=amount,
            card=cashin_service.MockCard(card_number, card_expiry, card_cvv, card_name),
            accepted=accepted,
        )
    except cashin_service.RemittanceError as exc:
        ctx = await _quote_context(db, user, beneficiary_id, zar_amount)
        if "quote" not in ctx:
            set_flash(request, str(exc), "danger")
            return RedirectResponse(url="/send", status_code=302)
        return _pay_page(request, user, ctx, error=str(exc), card_name=card_name)

    if txn.cashin_status.value == "failed":
        set_flash(request, f"Payment failed: {txn.cashin_failure_reason} Nothing will be sent.", "danger")
    else:
        set_flash(
            request,
            "Payment submitted. Your transfer starts as soon as the payment is confirmed.",
            "success",
        )
    return RedirectResponse(url=f"/transactions/{txn.id}", status_code=302)


# ── History (FR-CI-05, FR-MQ-05) ──────────────────────────────────────────────

@router.get("/transactions", response_class=HTMLResponse)
async def transaction_history(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    if user.is_admin:
        return RedirectResponse(url="/admin/cashin", status_code=302)
    return templates.TemplateResponse(
        "sender/transactions.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "transactions": await cashin_service.list_for_sender(db, user.id),
        },
    )


@router.get("/transactions/{txn_id}", response_class=HTMLResponse)
async def transaction_detail(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user:
        return RedirectResponse(url="/login", status_code=302)
    txn = await cashin_service.get_transaction(db, txn_id)
    # Visible to its sender, its recipient, and admins only.
    if txn is None or not (user.is_admin or user.id in (txn.sender_id, txn.recipient_user_id)):
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/transactions", status_code=302)
    return templates.TemplateResponse(
        "sender/transaction_detail.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "txn": txn,
            "is_sender": user.id == txn.sender_id,
        },
    )


# ── Mock payment service API (FR-CI-02) ───────────────────────────────────────

class CashInUpdate(BaseModel):
    status: Literal["received", "failed"]
    reason: str = ""


@router.patch("/transactions/{txn_id}/cashin")
async def update_cashin(
    txn_id: uuid.UUID,
    body: CashInUpdate,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """What a card processor's callback would call: mark a cash-in received or failed."""
    txn = await cashin_service.get_transaction(db, txn_id)
    if txn is None:
        raise HTTPException(status_code=404, detail="Transaction not found.")
    try:
        if body.status == "received":
            await cashin_service.mark_cashin_received(db, txn, admin)
        else:
            await cashin_service.mark_cashin_failed(db, txn, admin, body.reason)
    except cashin_service.RemittanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "id": str(txn.id),
        "cashin_status": txn.cashin_status.value,
        "cashin_updated_at": txn.cashin_updated_at.isoformat() if txn.cashin_updated_at else None,
        "settlement_status": txn.settlement_status.value,
    }
