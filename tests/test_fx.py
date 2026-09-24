"""FR-FX-01..08  FX quote engine."""
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beneficiary import PayoutCurrency
from app.services.auth_service import create_user
from app.services.beneficiary_service import create_beneficiary
from app.services.fx_service import (
    QUANT_UCTUSD,
    AmountTooSmall,
    calculate_quote,
    get_active_fee_config,
    get_market_rate,
    quote_for,
)
from app.services.kyc_service import approve_kyc, submit_kyc


# The worked example from BUILD_PLAN.md Phase 4 step 3.
EXAMPLE = dict(
    zar_send=Decimal("1000"),
    market_rate=Decimal("18.50"),
    fixed_fee_zar=Decimal("25"),
    percentage_fee=Decimal("0.015"),
    fx_margin=Decimal("0.02"),
    cashout_fee_percentage=Decimal("0.01"),
    cashout_fee_min_usd=Decimal("1"),
)


async def _approved_sender(db, suffix):
    user = await create_user(
        db, full_name=f"FX User {suffix}", email=f"fx{suffix}@test.com",
        mobile=f"+2782500{suffix}", password="Pass1234!",
    )
    admin = await create_user(
        db, full_name=f"FX Admin {suffix}", email=f"fxadm{suffix}@test.com",
        mobile=f"+2782600{suffix}", password="Pass1234!", is_admin=True,
    )
    sub = await submit_kyc(
        db, user,
        full_name=user.full_name, date_of_birth=date(1990, 1, 1),
        nationality="South African", id_number="9001010001087",
        residential_address="1 Test St", mobile=user.mobile,
        email=user.email, source_of_funds="Salary",
    )
    await approve_kyc(db, sub, admin)
    return user


# --- the worked example, to 6 dp ---

def test_worked_example_usd():
    q = calculate_quote(payout_currency=PayoutCurrency.USD, **EXAMPLE)

    assert q.transaction_fee == Decimal("40.00")
    assert q.net_zar_converted == Decimal("960.00")
    assert q.exchange_rate == Decimal("18.870000")
    assert q.uctusd_amount == Decimal("50.874404")


def test_worked_example_cashout_fee_and_usd_payout():
    q = calculate_quote(payout_currency=PayoutCurrency.USD, **EXAMPLE)

    # 1% of 50.874404 = 0.508744, which is below the $1 floor, so the floor wins.
    assert q.cashout_fee_estimate == Decimal("1.000000")
    assert q.payout_estimate == Decimal("49.874404")


def test_cashout_fee_percentage_wins_above_the_floor():
    # 1% of a large UCTUSD amount exceeds the $1 minimum.
    q = calculate_quote(**{**EXAMPLE, "zar_send": Decimal("50000")},
                        payout_currency=PayoutCurrency.USD)

    assert q.cashout_fee_estimate > Decimal("1")
    expected = (q.uctusd_amount * Decimal("0.01")).quantize(QUANT_UCTUSD, ROUND_HALF_UP)
    assert q.cashout_fee_estimate == expected


# --- the floor under the send amount (AUDIT #2) ---
#
# Without it: R10 quoted -0.802862 UCTUSD and R25.38 quoted 0.000000. Both were
# accepted, charged, and only failed at the worker — after the ZAR was booked and
# the daily allowance consumed, with no refund path.

@pytest.mark.parametrize("zar", ["0.01", "10", "25", "25.38"])
def test_quote_refuses_amounts_the_fee_consumes(zar):
    with pytest.raises(AmountTooSmall):
        calculate_quote(**{**EXAMPLE, "zar_send": Decimal(zar)}, payout_currency=PayoutCurrency.USD)


def test_quote_is_positive_immediately_above_the_break_even():
    """R25.39 is the first amount that leaves anything to convert."""
    q = calculate_quote(**{**EXAMPLE, "zar_send": Decimal("25.39")},
                        payout_currency=PayoutCurrency.USD)
    assert q.net_zar_converted > 0
    assert q.uctusd_amount > 0


def test_no_quote_can_produce_a_non_positive_uctusd_amount():
    """The property that matters: sweep the whole range around the break-even."""
    for cents in range(0, 6000):
        zar = Decimal(cents) / Decimal("100")
        try:
            q = calculate_quote(**{**EXAMPLE, "zar_send": zar},
                                payout_currency=PayoutCurrency.USD)
        except AmountTooSmall:
            continue
        assert q.uctusd_amount > 0, f"R{zar} quoted {q.uctusd_amount}"
        assert q.net_zar_converted > 0, f"R{zar} quoted net {q.net_zar_converted}"


async def test_quote_for_enforces_the_configured_minimum(db: AsyncSession, seed_fee_config):
    """The policy floor sits above the structural one and is admin-configurable."""
    with pytest.raises(AmountTooSmall) as exc:
        await quote_for(db, Decimal("49.99"), PayoutCurrency.USD)
    assert "R50.00" in str(exc.value)

    q = await quote_for(db, Decimal("50.00"), PayoutCurrency.USD)
    assert q.uctusd_amount > 0


def test_zar_payout_uses_the_market_rate_not_the_effective_rate():
    q = calculate_quote(payout_currency=PayoutCurrency.ZAR, **EXAMPLE)

    expected = ((q.uctusd_amount - q.cashout_fee_estimate) * Decimal("18.50")).quantize(
        Decimal("0.01"), ROUND_HALF_UP
    )
    assert q.payout_estimate == expected
    assert q.payout_currency == PayoutCurrency.ZAR


# --- Decimal, never float ---

def test_every_figure_is_decimal_not_float():
    q = calculate_quote(payout_currency=PayoutCurrency.USD, **EXAMPLE)

    for name in (
        "zar_amount", "transaction_fee", "net_zar_converted", "market_rate",
        "fx_margin", "exchange_rate", "uctusd_amount", "cashout_fee_estimate",
        "payout_estimate",
    ):
        value = getattr(q, name)
        assert isinstance(value, Decimal), f"{name} is {type(value)}, not Decimal"
        assert not isinstance(value, float)


def test_quantisation_is_exact():
    """Rates and UCTUSD carry exactly 6 dp; ZAR exactly 2 dp."""
    q = calculate_quote(payout_currency=PayoutCurrency.USD, **EXAMPLE)

    assert q.uctusd_amount.as_tuple().exponent == -6
    assert q.exchange_rate.as_tuple().exponent == -6
    assert q.transaction_fee.as_tuple().exponent == -2
    assert q.net_zar_converted.as_tuple().exponent == -2


def test_calculation_order_fee_comes_off_before_conversion():
    """net_zar is converted, not the gross — FR-FX-04 before FR-FX-05."""
    q = calculate_quote(payout_currency=PayoutCurrency.USD, **EXAMPLE)

    gross_converted = (Decimal("1000") / q.exchange_rate).quantize(QUANT_UCTUSD, ROUND_HALF_UP)
    assert q.uctusd_amount < gross_converted


# --- rate source & config plumbing ---

async def test_market_rate_comes_from_active_fee_config(db: AsyncSession, seed_fee_config):
    rate = await get_market_rate(db)
    assert rate == Decimal("18.500000")
    assert isinstance(rate, Decimal)


async def test_quote_for_reads_config_and_matches_worked_example(
    db: AsyncSession, seed_fee_config
):
    q = await quote_for(db, Decimal("1000"), PayoutCurrency.USD)

    assert q.transaction_fee == Decimal("40.00")
    assert q.exchange_rate == Decimal("18.870000")
    assert q.uctusd_amount == Decimal("50.874404")


async def test_changing_fee_config_changes_new_quotes(db: AsyncSession, seed_fee_config):
    """FR-FX-08: a config edit affects subsequent quotes."""
    before = await quote_for(db, Decimal("1000"), PayoutCurrency.USD)

    cfg = await get_active_fee_config(db)
    cfg.fixed_fee_zar = Decimal("50.00")
    db.add(cfg)
    await db.commit()

    after = await quote_for(db, Decimal("1000"), PayoutCurrency.USD)

    assert before.transaction_fee == Decimal("40.00")
    assert after.transaction_fee == Decimal("65.00")
    assert after.uctusd_amount < before.uctusd_amount


# --- GET /quote endpoint ---

async def test_quote_endpoint_returns_all_seven_figures(
    db: AsyncSession, client, seed_tiers, seed_fee_config
):
    user = await _approved_sender(db, "e1")
    ben = await create_beneficiary(
        db, user, full_name="Recipient One", email="r1@test.com",
        mobile="+27821110001", country="United States",
        payout_currency="USD", relationship="Parent",
    )

    await client.post("/login", data={"email": user.email, "password": "Pass1234!"})
    resp = await client.get(
        "/quote", params={"beneficiary_id": str(ben.id), "zar_amount": "1000"}
    )

    assert resp.status_code == 200
    body = resp.json()
    for field in (
        "zar_amount", "exchange_rate", "transaction_fee", "fx_margin",
        "uctusd_amount", "cashout_fee_estimate", "payout_estimate",
    ):
        assert field in body, f"missing display figure: {field}"

    assert Decimal(body["transaction_fee"]) == Decimal("40.00")
    assert Decimal(body["uctusd_amount"]) == Decimal("50.874404")
    assert body["limit_ok"] is True


async def test_quote_requires_login(client, seed_fee_config):
    import uuid

    resp = await client.get(
        "/quote", params={"beneficiary_id": str(uuid.uuid4()), "zar_amount": "1000"}
    )
    assert resp.status_code == 401


async def test_quote_rejects_unapproved_kyc_with_403(
    db: AsyncSession, client, seed_tiers, seed_fee_config
):
    import uuid

    user = await create_user(
        db, full_name="No KYC", email="nokyc_fx@test.com",
        mobile="+27827770001", password="Pass1234!",
    )
    await client.post("/login", data={"email": user.email, "password": "Pass1234!"})

    resp = await client.get(
        "/quote", params={"beneficiary_id": str(uuid.uuid4()), "zar_amount": "1000"}
    )
    assert resp.status_code == 403
    assert "KYC" in resp.json()["detail"]


async def test_quote_rejects_someone_elses_beneficiary(
    db: AsyncSession, client, seed_tiers, seed_fee_config
):
    owner = await _approved_sender(db, "e2")
    other = await _approved_sender(db, "e3")
    ben = await create_beneficiary(
        db, owner, full_name="Owned Recipient", email="r2@test.com",
        mobile="+27821110002", country="United States",
        payout_currency="USD", relationship="Friend",
    )

    await client.post("/login", data={"email": other.email, "password": "Pass1234!"})
    resp = await client.get(
        "/quote", params={"beneficiary_id": str(ben.id), "zar_amount": "1000"}
    )
    assert resp.status_code == 404


async def test_quote_flags_an_over_limit_amount(
    db: AsyncSession, client, seed_tiers, seed_fee_config
):
    """FR-LIM-03: over-limit is surfaced on the quote, before any payment step."""
    user = await _approved_sender(db, "e4")
    ben = await create_beneficiary(
        db, user, full_name="Recipient Three", email="r3@test.com",
        mobile="+27821110003", country="United States",
        payout_currency="USD", relationship="Sibling",
    )

    await client.post("/login", data={"email": user.email, "password": "Pass1234!"})
    resp = await client.get(
        "/quote", params={"beneficiary_id": str(ben.id), "zar_amount": "25000"}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["limit_ok"] is False
    assert "Daily limit exceeded" in body["limit_reason"]
