"""
FR-FX-01..08  FX quote engine.

All money is Decimal — never float. Quantisation is fixed by QUANT_*:
ZAR 2 dp, UCTUSD 6 dp, rates 6 dp. Rounding is ROUND_HALF_UP throughout.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.beneficiary import PayoutCurrency
from app.models.platform_config import FeeConfig

QUANT_ZAR = Decimal("0.01")
QUANT_UCTUSD = Decimal("0.000001")
QUANT_RATE = Decimal("0.000001")


def _q(value: Decimal, exp: Decimal) -> Decimal:
    return value.quantize(exp, rounding=ROUND_HALF_UP)


class FXConfigError(RuntimeError):
    """Raised when no active fee_config row exists — the platform cannot quote."""


@dataclass(frozen=True)
class Quote:
    """The seven display figures (FR-FX-07) plus the inputs behind them."""

    zar_amount: Decimal
    transaction_fee: Decimal
    net_zar_converted: Decimal
    market_rate: Decimal
    fx_margin: Decimal
    exchange_rate: Decimal
    uctusd_amount: Decimal
    cashout_fee_estimate: Decimal
    payout_estimate: Decimal
    payout_currency: PayoutCurrency


async def get_active_fee_config(db: AsyncSession) -> Optional[FeeConfig]:
    """The single active fee/margin configuration row."""
    result = await db.execute(
        select(FeeConfig)
        .where(FeeConfig.is_active.is_(True))
        .order_by(FeeConfig.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_market_rate(db: AsyncSession, fee_config: Optional[FeeConfig] = None) -> Decimal:
    """Mid-market ZAR per 1 USD (FR-FX-01).

    Indirection point for the rate source: today it reads a configured mock value,
    tomorrow it can call a live FX API. Callers must not care which — they always
    get a quantised Decimal.
    """
    source = (settings.fx_rate_source or "config").lower()

    if source == "config":
        if fee_config is None:
            fee_config = await get_active_fee_config(db)
        if fee_config is None:
            raise FXConfigError("No active fee_config row — cannot determine a market rate.")
        return _q(Decimal(fee_config.market_rate_zar_per_usd), QUANT_RATE)

    if source == "static":
        # Env-pinned rate, for tests and offline demos.
        return _q(Decimal(str(settings.fx_static_market_rate)), QUANT_RATE)

    raise FXConfigError(f"Unsupported FX rate source: {source!r}")


def calculate_quote(
    zar_send: Decimal,
    market_rate: Decimal,
    fixed_fee_zar: Decimal,
    percentage_fee: Decimal,
    fx_margin: Decimal,
    cashout_fee_percentage: Decimal,
    cashout_fee_min_usd: Decimal,
    payout_currency: PayoutCurrency,
) -> Quote:
    """Pure calculation, in the exact order mandated by BUILD_PLAN.md Phase 4 step 3.

    Worked example: zar_send=1000, fixed=25, pct=0.015, margin=0.02, market=18.50
    -> fee=40.00, net=960.00, effective=18.870000, uctusd=50.874404.
    """
    zar_send = _q(Decimal(zar_send), QUANT_ZAR)
    market_rate = _q(Decimal(market_rate), QUANT_RATE)

    # FR-FX-02
    transaction_fee = _q(Decimal(fixed_fee_zar) + (Decimal(percentage_fee) * zar_send), QUANT_ZAR)
    # FR-FX-03
    effective_rate = _q(market_rate * (Decimal("1") + Decimal(fx_margin)), QUANT_RATE)
    # FR-FX-04
    net_zar = _q(zar_send - transaction_fee, QUANT_ZAR)
    # FR-FX-05
    uctusd_amount = _q(net_zar / effective_rate, QUANT_UCTUSD)
    # FR-FX-06
    cashout_fee_estimate = _q(
        max(Decimal(cashout_fee_percentage) * uctusd_amount, Decimal(cashout_fee_min_usd)),
        QUANT_UCTUSD,
    )
    net_uctusd = uctusd_amount - cashout_fee_estimate
    if payout_currency == PayoutCurrency.USD:
        payout_estimate = _q(net_uctusd, QUANT_UCTUSD)
    else:
        payout_estimate = _q(net_uctusd * market_rate, QUANT_ZAR)

    return Quote(
        zar_amount=zar_send,
        transaction_fee=transaction_fee,
        net_zar_converted=net_zar,
        market_rate=market_rate,
        fx_margin=_q(Decimal(fx_margin), QUANT_RATE),
        exchange_rate=effective_rate,
        uctusd_amount=uctusd_amount,
        cashout_fee_estimate=cashout_fee_estimate,
        payout_estimate=payout_estimate,
        payout_currency=payout_currency,
    )


async def quote_for(
    db: AsyncSession, zar_send: Decimal, payout_currency: PayoutCurrency
) -> Quote:
    """Load the active config + market rate and produce a quote."""
    fee_config = await get_active_fee_config(db)
    if fee_config is None:
        raise FXConfigError("No active fee_config row — the platform cannot issue quotes.")

    market_rate = await get_market_rate(db, fee_config)
    return calculate_quote(
        zar_send=zar_send,
        market_rate=market_rate,
        fixed_fee_zar=fee_config.fixed_fee_zar,
        percentage_fee=fee_config.percentage_fee,
        fx_margin=fee_config.fx_margin,
        cashout_fee_percentage=fee_config.cashout_fee_percentage,
        cashout_fee_min_usd=fee_config.cashout_fee_min_usd,
        payout_currency=payout_currency,
    )
