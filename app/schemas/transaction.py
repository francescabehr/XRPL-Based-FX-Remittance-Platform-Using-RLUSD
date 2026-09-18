"""FR-FX-07  Quote request/response schemas."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models.beneficiary import PayoutCurrency


class QuoteRequest(BaseModel):
    beneficiary_id: uuid.UUID
    zar_amount: Decimal = Field(gt=0, description="ZAR the sender wants to send.")


class QuoteResponse(BaseModel):
    """All seven display figures on one screen (FR-FX-07), plus limit context."""

    model_config = ConfigDict(from_attributes=True)

    beneficiary_id: uuid.UUID
    beneficiary_name: str
    payout_currency: PayoutCurrency

    # --- the seven figures ---
    zar_amount: Decimal
    exchange_rate: Decimal          # effective rate applied (market * (1 + margin))
    transaction_fee: Decimal
    fx_margin: Decimal
    uctusd_amount: Decimal
    cashout_fee_estimate: Decimal
    payout_estimate: Decimal

    # --- supporting detail ---
    market_rate: Decimal
    net_zar_converted: Decimal

    # --- limit check (FR-LIM-03): shown before any payment step ---
    limit_ok: bool
    limit_reason: Optional[str] = None
    daily_remaining: Decimal
    monthly_remaining: Decimal
