"""FR-FX-07  GET /quote — authenticated, KYC-approved senders only."""
import uuid
from decimal import Decimal, InvalidOperation
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies import get_current_user
from app.models.user import KYCStatus, User
from app.schemas.transaction import QuoteResponse
from app.services.beneficiary_service import get_beneficiary
from app.services.fx_service import FXConfigError, quote_for
from app.services.limit_service import check_limit

router = APIRouter()


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
