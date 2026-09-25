import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_flash, require_admin, set_flash
from app.models.transaction import CashInStatus, SettlementStatus
from app.services import cashin_service, cashout_service, fx_service
from app.services.kyc_service import (
    KYCReviewError,
    approve_kyc,
    count_pending_submissions,
    get_pending_submissions,
    get_submission_by_id,
    reject_kyc,
)
from app.services.limit_service import day_start_utc, display_tz, get_all_tiers, get_tier_by_id, update_tier
from app.templating import make_templates

router = APIRouter(prefix="/admin")
templates = make_templates()


# ── Overview (admin landing page) ─────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def overview(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    today_start = day_start_utc(datetime.now(display_tz()).date())
    return templates.TemplateResponse(
        "admin/overview.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "pending_kyc": await count_pending_submissions(db),
            "pending_cashins": await cashin_service.count_pending_cashins(db),
            "cashouts": await cashout_service.count_open(db),
            "failed_settlements": await cashin_service.count_failed_settlements(db),
            "settled_today": await cashin_service.count_settled_since(db, today_start),
        },
    )


@router.get("/kyc", response_class=HTMLResponse)
async def kyc_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    submissions = await get_pending_submissions(db)
    return templates.TemplateResponse(
        "admin/kyc_queue.html",
        {
            "request": request,
            "user": user,
            "submissions": submissions,
            "flash": get_flash(request),
        },
    )


@router.get("/kyc/{submission_id}", response_class=HTMLResponse)
async def kyc_detail(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    return templates.TemplateResponse(
        "admin/kyc_detail.html",
        {
            "request": request,
            "user": user,
            "submission": submission,
            "flash": get_flash(request),
        },
    )


@router.post("/kyc/{submission_id}/approve")
async def kyc_approve(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    try:
        await approve_kyc(db, submission, user)
    except KYCReviewError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    set_flash(request, f"KYC approved for {submission.user.full_name}.", "success")
    return RedirectResponse(url="/admin/kyc", status_code=302)


@router.post("/kyc/{submission_id}/reject")
async def kyc_reject(
    submission_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    reason: str = Form(...),
):
    submission = await get_submission_by_id(db, submission_id)
    if not submission:
        set_flash(request, "Submission not found.", "danger")
        return RedirectResponse(url="/admin/kyc", status_code=302)

    try:
        await reject_kyc(db, submission, user, reason)
    except KYCReviewError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/kyc", status_code=302)
    except ValueError as exc:
        set_flash(request, str(exc), "danger")
        return RedirectResponse(url=f"/admin/kyc/{submission_id}", status_code=302)

    set_flash(request, f"KYC rejected for {submission.user.full_name}.", "warning")
    return RedirectResponse(url="/admin/kyc", status_code=302)


# ── Fee & Limit Configuration (FR-LIM-05, FR-FX-08) ──────────────────────────

@router.get("/config", response_class=HTMLResponse)
async def config_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    tiers = await get_all_tiers(db)
    return templates.TemplateResponse(
        "admin/config.html",
        {
            "request": request,
            "user": user,
            "tiers": tiers,
            "fee_config": await fx_service.get_active_fee_config(db),
            "flash": get_flash(request),
        },
    )


@router.post("/config/tiers/{tier_id}")
async def update_tier_limits(
    tier_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    daily_limit_zar: str = Form(...),
    monthly_limit_zar: str = Form(...),
):
    from decimal import Decimal, InvalidOperation
    try:
        daily = Decimal(daily_limit_zar)
        monthly = Decimal(monthly_limit_zar)
        if daily < 0 or monthly < 0:
            raise ValueError("Limits cannot be negative.")
    except (InvalidOperation, ValueError) as exc:
        set_flash(request, f"Invalid value: {exc}", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    tier = await get_tier_by_id(db, tier_id)
    if not tier:
        set_flash(request, "Tier not found.", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    await update_tier(db, tier, daily, monthly)
    set_flash(request, f"Limits for '{tier.tier_name}' updated.", "success")
    return RedirectResponse(url="/admin/config", status_code=302)


def _decimal_field(label: str, raw: str) -> Decimal:
    """Parse one money/rate form field, refusing junk and negatives."""
    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{label} must be a number.") from None
    if not value.is_finite():
        raise ValueError(f"{label} must be a number.")
    if value < 0:
        raise ValueError(f"{label} cannot be negative.")
    return value


@router.post("/config/fees")
async def update_fees(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    fixed_fee_zar: str = Form(...),
    percentage_fee: str = Form(...),
    fx_margin: str = Form(...),
    cashout_fee_percentage: str = Form(...),
    cashout_fee_min_usd: str = Form(...),
    market_rate_zar_per_usd: str = Form(...),
    min_send_zar: str = Form(...),
):
    """FR-ADM-06 / FR-FX-08: edit the active fee row. Never retroactive."""
    fields = {
        "fixed_fee_zar": ("Fixed fee", fixed_fee_zar),
        "percentage_fee": ("Percentage fee", percentage_fee),
        "fx_margin": ("FX margin", fx_margin),
        "cashout_fee_percentage": ("Cash-out fee", cashout_fee_percentage),
        "cashout_fee_min_usd": ("Minimum cash-out fee", cashout_fee_min_usd),
        "market_rate_zar_per_usd": ("Mock market rate", market_rate_zar_per_usd),
        "min_send_zar": ("Minimum send amount", min_send_zar),
    }
    try:
        values = {name: _decimal_field(label, raw) for name, (label, raw) in fields.items()}
    except ValueError as exc:
        set_flash(request, f"Invalid value: {exc}", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    if values["market_rate_zar_per_usd"] <= 0:
        set_flash(request, "Invalid value: Mock market rate must be greater than zero.", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    # A minimum at or below the fixed fee would let a send through that the fee
    # consumes entirely — the floor exists precisely to make that unreachable.
    if values["min_send_zar"] <= values["fixed_fee_zar"]:
        set_flash(
            request,
            "Invalid value: Minimum send amount must be greater than the fixed fee.",
            "danger",
        )
        return RedirectResponse(url="/admin/config", status_code=302)

    cfg = await fx_service.get_active_fee_config(db)
    if not cfg:
        set_flash(request, "No active fee configuration row exists to edit.", "danger")
        return RedirectResponse(url="/admin/config", status_code=302)

    await fx_service.update_fee_config(db, cfg, **values)
    set_flash(request, "Fee configuration saved. It applies to quotes generated from now on.", "success")
    return RedirectResponse(url="/admin/config", status_code=302)


# ── Cash-in queue (FR-CI-02, FR-ADM-03) ──────────────────────────────────────

@router.get("/cashin", response_class=HTMLResponse)
async def cashin_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/cashin_queue.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "transactions": await cashin_service.list_pending_cashins(db),
        },
    )


@router.post("/cashin/{txn_id}/received")
async def cashin_received(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    txn = await cashin_service.get_transaction(db, txn_id)
    if not txn:
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/admin/cashin", status_code=302)
    try:
        await cashin_service.mark_cashin_received(db, txn, user)
    except cashin_service.RemittanceError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/cashin", status_code=302)

    if txn.settlement_status.value == "failed":
        set_flash(request, f"Cash-in confirmed, but settlement could not be queued: {txn.xrpl_error_reason}", "danger")
    else:
        set_flash(request, f"Cash-in confirmed for R{txn.zar_amount:,.2f}. Settlement queued.", "success")
    return RedirectResponse(url="/admin/cashin", status_code=302)


@router.post("/cashin/{txn_id}/failed")
async def cashin_failed(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    reason: str = Form(""),
):
    txn = await cashin_service.get_transaction(db, txn_id)
    if not txn:
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/admin/cashin", status_code=302)
    try:
        await cashin_service.mark_cashin_failed(db, txn, user, reason)
    except cashin_service.RemittanceError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/cashin", status_code=302)
    set_flash(request, "Cash-in marked as failed. Nothing will be sent.", "warning")
    return RedirectResponse(url="/admin/cashin", status_code=302)


# ── Settlement monitor (FR-MQ-06, FR-ADM-05) ─────────────────────────────────

@router.get("/settlements", response_class=HTMLResponse)
async def settlement_monitor(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/settlements.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "transactions": await cashin_service.list_settlement_issues(db),
        },
    )


@router.post("/settlements/{txn_id}/retry")
async def settlement_retry(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    txn = await cashin_service.get_transaction(db, txn_id)
    if not txn:
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/admin/settlements", status_code=302)
    try:
        if txn.settlement_status.value == "queued":
            await cashin_service.requeue_stuck(db, txn)
            outcome = "requeued"
        else:
            outcome = await cashin_service.retry_settlement(db, txn)
    except cashin_service.RemittanceError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/settlements", status_code=302)
    except Exception as exc:  # noqa: BLE001 — e.g. Testnet unreachable during the ledger check
        set_flash(request, f"Could not check the ledger ({type(exc).__name__}). Try again shortly.", "danger")
        return RedirectResponse(url="/admin/settlements", status_code=302)

    if outcome == "reconciled":
        set_flash(request, "The earlier payment had succeeded on the ledger. Marked completed; nothing re-sent.", "success")
    elif txn.settlement_status.value == "failed":
        set_flash(request, f"Could not queue the retry: {txn.xrpl_error_reason}", "danger")
    else:
        set_flash(request, "Settlement re-queued.", "success")
    return RedirectResponse(url="/admin/settlements", status_code=302)


# ── Cash-out approval queue (FR-CO-05, FR-ADM-05) ────────────────────────────

@router.get("/cashout", response_class=HTMLResponse)
async def cashout_queue(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    return templates.TemplateResponse(
        "admin/cashout_queue.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "requests": await cashout_service.list_open(db),
        },
    )


@router.post("/cashout/{request_id}/approve")
async def cashout_approve(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """FR-CO-05/06: reserve the UCTUSD and queue the on-chain burn."""
    req = await cashout_service.get_request(db, request_id)
    if not req:
        set_flash(request, "Cash-out request not found.", "danger")
        return RedirectResponse(url="/admin/cashout", status_code=302)
    try:
        await cashout_service.approve(db, req, user)
    except cashout_service.CashOutError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/cashout", status_code=302)

    if req.status.value == "failed":
        set_flash(request, f"Could not queue the burn: {req.failure_reason}", "danger")
    else:
        set_flash(
            request,
            f"Approved. {req.uctusd_amount:,.6f} UCTUSD reserved and the burn is queued.",
            "success",
        )
    return RedirectResponse(url="/admin/cashout", status_code=302)


@router.post("/cashout/{request_id}/reject")
async def cashout_reject(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    reason: str = Form(""),
):
    req = await cashout_service.get_request(db, request_id)
    if not req:
        set_flash(request, "Cash-out request not found.", "danger")
        return RedirectResponse(url="/admin/cashout", status_code=302)
    try:
        await cashout_service.reject(db, req, user, reason)
    except cashout_service.CashOutError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/cashout", status_code=302)
    set_flash(request, "Cash-out rejected. No UCTUSD was reserved or burned.", "warning")
    return RedirectResponse(url="/admin/cashout", status_code=302)


@router.post("/cashout/{request_id}/reconcile")
async def cashout_reconcile(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """Resolve a held cash-out against the ledger — never against the clock."""
    req = await cashout_service.get_request(db, request_id)
    if not req:
        set_flash(request, "Cash-out request not found.", "danger")
        return RedirectResponse(url="/admin/cashout", status_code=302)
    try:
        outcome = await cashout_service.reconcile(db, req)
    except cashout_service.CashOutError as exc:
        set_flash(request, str(exc), "warning")
        return RedirectResponse(url="/admin/cashout", status_code=302)
    except Exception as exc:  # noqa: BLE001 — e.g. Testnet unreachable
        set_flash(request, f"Could not reach the ledger ({type(exc).__name__}). Try again shortly.", "danger")
        return RedirectResponse(url="/admin/cashout", status_code=302)

    if outcome == "completed":
        set_flash(request, "The burn had succeeded on-ledger. Marked completed; the payout is simulated.", "success")
    elif outcome == "failed":
        set_flash(request, "The burn provably never landed. Marked failed and the reserved UCTUSD was restored.", "warning")
    else:
        set_flash(
            request,
            "Still unresolved: the burn could yet be validated, so the reserve stays held. "
            "Nothing was changed.",
            "info",
        )
    return RedirectResponse(url="/admin/cashout", status_code=302)


# ── Transaction monitor (FR-ADM-04, FR-ADM-07) ───────────────────────────────

def _enum_filter(enum_cls, raw: Optional[str]):
    """A status filter value, or None for 'all'. Unknown values fall back to all."""
    if not raw:
        return None
    try:
        return enum_cls(raw)
    except ValueError:
        return None


def _date_filter(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _aml_filter(raw: Optional[str]) -> Optional[bool]:
    return {"1": True, "flagged": True, "0": False, "clear": False}.get((raw or "").strip().lower())


@router.get("/transactions", response_class=HTMLResponse)
async def transaction_monitor(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    cashin: Optional[str] = Query(None),
    settlement: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    q: Optional[str] = Query(None),
    aml: Optional[str] = Query(None),
):
    """FR-ADM-04: every remittance, filterable by status, date range, user and AML flag.

    Scope is the `transactions` table (money in from senders). Cash-outs are a
    separate flow and live on /admin/cashout, which this screen links to.
    """
    filters = {
        "cashin_status": _enum_filter(CashInStatus, cashin),
        "settlement_status": _enum_filter(SettlementStatus, settlement),
        "date_from": _date_filter(date_from),
        "date_to": _date_filter(date_to),
        "user_query": (q or "").strip() or None,
        "aml_flagged": _aml_filter(aml),
    }
    transactions = await cashin_service.list_transactions(db, **filters)

    return templates.TemplateResponse(
        "admin/transactions.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "transactions": transactions,
            "truncated": len(transactions) >= cashin_service.MONITOR_LIMIT,
            "monitor_limit": cashin_service.MONITOR_LIMIT,
            # Echoed back so the form keeps what the admin typed, invalid parts included.
            "selected": {
                "cashin": cashin or "",
                "settlement": settlement or "",
                "date_from": date_from or "",
                "date_to": date_to or "",
                "q": q or "",
                "aml": aml or "",
            },
            "cashin_statuses": [s.value for s in CashInStatus],
            "settlement_statuses": [s.value for s in SettlementStatus],
        },
    )


@router.get("/transactions/{txn_id}", response_class=HTMLResponse)
async def transaction_detail(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
):
    """FR-ADM-04: drill-down — cash-in, settlement, XRPL hash and validation result."""
    txn = await cashin_service.get_transaction(db, txn_id)
    if not txn:
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/admin/transactions", status_code=302)

    return templates.TemplateResponse(
        "admin/transaction_detail.html",
        {
            "request": request,
            "user": user,
            "flash": get_flash(request),
            "txn": txn,
        },
    )


@router.post("/transactions/{txn_id}/aml")
async def transaction_set_aml(
    txn_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_admin),
    flagged: str = Form(...),
):
    """FR-ADM-07: raise or clear the AML review flag. Review-only — no money moves."""
    txn = await cashin_service.get_transaction(db, txn_id)
    if not txn:
        set_flash(request, "Transaction not found.", "danger")
        return RedirectResponse(url="/admin/transactions", status_code=302)

    flag = flagged.strip().lower() in {"1", "true", "yes", "on"}
    await cashin_service.set_aml_flag(db, txn, flag)
    set_flash(
        request,
        "Flagged for AML review." if flag else "AML flag cleared.",
        "warning" if flag else "success",
    )
    return RedirectResponse(url=f"/admin/transactions/{txn_id}", status_code=302)
