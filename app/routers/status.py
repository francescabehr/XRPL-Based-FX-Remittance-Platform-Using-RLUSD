"""Live status for polling pages (UI_REDESIGN.md §7.4): read-only JSON.

GET /status?t=<transaction id>&c=<cash-out id> (each repeatable, 50 ids at most)
returns the current state of every item the signed-in user may see, with the
badge and hash already rendered by the same macros the pages use, so a polled
update looks exactly like a fresh page load. Items the user may not see are left
out rather than refused, and invalid ids are ignored. Nothing here writes.
"""
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.cashout import CashOutRequest, CashOutStatus
from app.models.transaction import CashInStatus, SettlementStatus, Transaction
from app.models.user import User
from app.services import xrpl_service
from app.templating import make_templates

router = APIRouter()
templates = make_templates()

MAX_IDS = 50


def _parse_ids(t: list[str], c: list[str]) -> tuple[list[uuid.UUID], list[uuid.UUID]]:
    """Valid, de-duplicated ids in request order; the first MAX_IDS across both lists."""
    seen: set[tuple[str, uuid.UUID]] = set()
    txn_ids: list[uuid.UUID] = []
    cashout_ids: list[uuid.UUID] = []
    for kind, raw_ids, out in (("t", t, txn_ids), ("c", c, cashout_ids)):
        for raw in raw_ids:
            if len(seen) >= MAX_IDS:
                return txn_ids, cashout_ids
            try:
                value = uuid.UUID(raw)
            except (ValueError, AttributeError, TypeError):
                continue
            if (kind, value) in seen:
                continue
            seen.add((kind, value))
            out.append(value)
    return txn_ids, cashout_ids


def _macros(name: str):
    return templates.env.get_template(name).module


def _transaction_state(txn: Transaction, status_macros, xrpl_macros) -> dict:
    final = txn.cashin_status == CashInStatus.failed or txn.settlement_status in (
        SettlementStatus.completed,
        SettlementStatus.failed,
    )
    return {
        "cashin_status": txn.cashin_status.value,
        "settlement_status": txn.settlement_status.value,
        "final": final,
        "badge_html": str(status_macros.transaction_badge(txn)),
        "hash": txn.xrpl_tx_hash,
        "hash_html": str(xrpl_macros.tx_hash(txn.xrpl_tx_hash)),
        "failure_reason": txn.failure_reason,
    }


def _cashout_state(req: CashOutRequest, status_macros, xrpl_macros) -> dict:
    awaiting = req.awaiting_ledger_confirmation
    return {
        "status": req.status.value,
        "awaiting_ledger": awaiting,
        "final": req.status in (CashOutStatus.completed, CashOutStatus.failed),
        "badge_html": str(status_macros.cashout_badge(req)),
        "hash": req.xrpl_burn_tx_hash,
        "hash_html": str(xrpl_macros.tx_hash(req.xrpl_burn_tx_hash)),
        # The outcome_unknown marker is internal; the badge already says what it means.
        "failure_reason": None if awaiting else req.failure_reason,
    }


@router.get("/status")
async def live_status(
    t: list[str] = Query(default=[]),
    c: list[str] = Query(default=[]),
    db: AsyncSession = Depends(get_db),
    user: Optional[User] = Depends(get_current_user),
):
    if not user:
        return JSONResponse({"detail": "Not signed in."}, status_code=401)

    txn_ids, cashout_ids = _parse_ids(t, c)
    status_macros = _macros("components/status.html")
    xrpl_macros = _macros("components/xrpl.html")

    # populate_existing: the workers write these rows in their own sessions.
    transactions: dict[str, dict] = {}
    if txn_ids:
        stmt = select(Transaction).where(Transaction.id.in_(txn_ids))
        if not user.is_admin:
            stmt = stmt.where(or_(Transaction.sender_id == user.id, Transaction.recipient_user_id == user.id))
        rows = (await db.execute(stmt.execution_options(populate_existing=True))).scalars().all()
        transactions = {str(row.id): _transaction_state(row, status_macros, xrpl_macros) for row in rows}

    cashouts: dict[str, dict] = {}
    if cashout_ids:
        stmt = select(CashOutRequest).where(CashOutRequest.id.in_(cashout_ids))
        if not user.is_admin:
            stmt = stmt.where(CashOutRequest.recipient_user_id == user.id)
        rows = (await db.execute(stmt.execution_options(populate_existing=True))).scalars().all()
        cashouts = {str(row.id): _cashout_state(row, status_macros, xrpl_macros) for row in rows}

    wallet_balance = None
    if not user.is_admin:
        wallet = await xrpl_service.get_wallet_for_user(db, user.id)
        if wallet is not None:
            await db.refresh(wallet)
            wallet_balance = str(wallet.balance_uctusd)

    items = list(transactions.values()) + list(cashouts.values())
    return JSONResponse(
        {
            "transactions": transactions,
            "cashouts": cashouts,
            "wallet_balance": wallet_balance,
            "all_final": all(item["final"] for item in items),
        },
        headers={"Cache-Control": "no-store"},
    )
