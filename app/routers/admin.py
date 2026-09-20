import uuid

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_flash, require_admin, set_flash
from app.services import cashin_service, cashout_service
from app.services.kyc_service import (
    approve_kyc,
    get_pending_submissions,
    get_submission_by_id,
    reject_kyc,
)
from app.services.limit_service import get_all_tiers, get_tier_by_id, update_tier
from app.templating import make_templates

router = APIRouter(prefix="/admin")
templates = make_templates()


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

    await approve_kyc(db, submission, user)
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
