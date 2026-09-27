"""FR-ADM-01..07  Admin portal: gating, transaction monitor, fee config, AML flag."""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.dependencies import get_current_user, require_admin
from app.main import app
from app.models.transaction import CashInStatus, SettlementStatus
from app.services import cashin_service, fx_service
from app.services.auth_service import create_user
from app.services.beneficiary_service import PayoutCurrency
from app.services.limit_service import display_tz
from tests.remit_helpers import approved_sender, logged_in, remittance

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")


# --- helpers ---

def _admin_routes() -> list[tuple[str, str]]:
    """Every (method, path) the app exposes under /admin."""
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/admin"):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                routes.append((method, route.path))
    return sorted(routes)


def _concrete(path: str) -> str:
    """Substitute a real-looking UUID for each path parameter."""
    parts = [str(uuid.uuid4()) if p.startswith("{") else p for p in path.split("/")]
    return "/".join(parts)


async def admin_user(db: AsyncSession):
    tag = uuid.uuid4().hex[:8]
    return await create_user(
        db,
        full_name=f"Admin {tag}",
        email=f"adm{tag}@test.com",
        mobile=f"+2776{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}",
        password="Pass1234!",
        is_admin=True,
    )


async def _settled(db, txn, *, tx_hash="ABC123", flagged=False):
    """Move a transaction to received + completed so monitor filters have variety."""
    txn.cashin_status = CashInStatus.received
    txn.settlement_status = SettlementStatus.completed
    txn.xrpl_tx_hash = tx_hash
    txn.settled_at = datetime.now(timezone.utc)
    txn.aml_flagged = flagged
    await db.commit()
    return txn


# --- FR-ADM-01: gating ---

async def test_non_admin_gets_403_on_admin_route(client: AsyncClient, db: AsyncSession):
    user = await create_user(
        db,
        full_name="Not An Admin",
        email="notadmin@example.com",
        mobile="+27900000099",
        password="TestPass1!",
    )

    async def _override():
        return user

    app.dependency_overrides[get_current_user] = _override
    try:
        response = await client.get("/admin/kyc", follow_redirects=False)
        assert response.status_code == 403
    finally:
        del app.dependency_overrides[get_current_user]


async def test_unauthenticated_redirected_from_admin(client: AsyncClient):
    async def _override():
        return None

    app.dependency_overrides[get_current_user] = _override
    try:
        response = await client.get("/admin/kyc", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["location"]
    finally:
        del app.dependency_overrides[get_current_user]


def test_every_admin_route_is_behind_require_admin():
    """dependencies.py:require_admin is the single enforcement point (FR-ADM-01)."""
    unguarded = [
        (m, r.path)
        for r in app.routes
        if isinstance(r, APIRoute) and r.path.startswith("/admin")
        for m in (r.methods - {"HEAD", "OPTIONS"})
        if require_admin not in [d.call for d in r.dependant.dependencies]
    ]
    assert unguarded == []


async def test_non_admin_gets_403_on_every_admin_route(client: AsyncClient, db: AsyncSession):
    """Senders and recipients are refused everywhere under /admin, old routes included."""
    sender = await approved_sender(db)
    routes = _admin_routes()
    assert len(routes) >= 16  # sanity: the whole portal, not a stale subset

    with logged_in(sender):
        for method, path in routes:
            response = await client.request(method, _concrete(path), data={}, follow_redirects=False)
            assert response.status_code == 403, f"{method} {path} returned {response.status_code}"


# --- FR-ADM-04: transaction monitor ---

async def test_monitor_lists_transactions(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db, "1000")
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get("/admin/transactions")

    assert response.status_code == 200
    assert sender.full_name in response.text


async def test_monitor_filters_by_user(client: AsyncClient, db: AsyncSession):
    mine, my_sender, _ = await remittance(db, "1000")
    other, other_sender, _ = await remittance(db, "1200")
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get("/admin/transactions", params={"q": my_sender.email})

    assert my_sender.full_name in response.text
    assert other_sender.full_name not in response.text


async def test_monitor_filters_by_status(client: AsyncClient, db: AsyncSession):
    pending, pending_sender, _ = await remittance(db, "1000")
    done, done_sender, _ = await remittance(db, "1100")
    await _settled(db, done)
    admin = await admin_user(db)

    with logged_in(admin):
        completed = await client.get("/admin/transactions", params={"settlement": "completed"})
        awaiting = await client.get("/admin/transactions", params={"cashin": "pending"})

    assert done_sender.full_name in completed.text
    assert pending_sender.full_name not in completed.text
    assert pending_sender.full_name in awaiting.text
    assert done_sender.full_name not in awaiting.text


@pytest.mark.parametrize(
    "created_at",
    [
        None,                                                    # the real clock
        datetime(2026, 9, 25, 22, 30, tzinfo=timezone.utc),      # 00:30 SAST: UTC is still the day before
        datetime(2026, 9, 25, 23, 59, tzinfo=timezone.utc),      # 01:59 SAST
    ],
    ids=["now", "00:30-SAST", "01:59-SAST"],
)
async def test_monitor_filters_by_date_range(db: AsyncSession, created_at):
    """Date bounds are DISPLAY_TIMEZONE days (the day the admin reads on screen)
    and the end day is inclusive. Pinned creation times just after local midnight
    are the case a UTC "today" gets wrong."""
    txn, sender, _ = await remittance(db, "1000")
    if created_at is not None:
        txn.created_at = created_at
        await db.commit()
    today = (created_at or datetime.now(timezone.utc)).astimezone(display_tz()).date()

    inside = await cashin_service.list_transactions(db, date_from=today, date_to=today)
    before = await cashin_service.list_transactions(db, date_to=today - timedelta(days=1))
    after = await cashin_service.list_transactions(db, date_from=today + timedelta(days=1))

    assert txn.id in [t.id for t in inside]
    assert txn.id not in [t.id for t in before]
    assert txn.id not in [t.id for t in after]


async def test_monitor_filters_combine(db: AsyncSession):
    """Filters are AND-ed: the right user but the wrong status matches nothing."""
    txn, sender, _ = await remittance(db, "1000")

    match = await cashin_service.list_transactions(
        db, user_query=sender.email, cashin_status=CashInStatus.pending
    )
    mismatch = await cashin_service.list_transactions(
        db, user_query=sender.email, cashin_status=CashInStatus.received
    )

    assert [t.id for t in match] == [txn.id]
    assert mismatch == []


async def test_monitor_ignores_an_unparseable_filter(client: AsyncClient, db: AsyncSession):
    """A junk status or date falls back to 'all' rather than erroring."""
    txn, sender, _ = await remittance(db, "1000")
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get(
            "/admin/transactions", params={"settlement": "banana", "date_from": "not-a-date"}
        )

    assert response.status_code == 200
    assert sender.full_name in response.text


async def test_monitor_detail_shows_settlement_and_hash(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db, "1000")
    await _settled(db, txn, tx_hash="DEADBEEF00112233")
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get(f"/admin/transactions/{txn.id}")

    assert response.status_code == 200
    assert "DEADBEEF00" in response.text          # hash, linked to the explorer
    assert "testnet.xrpl.org" in response.text
    assert "tesSUCCESS" in response.text          # validation result
    assert str(txn.idempotency_key) in response.text


async def test_monitor_detail_shows_failure_reason(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db, "1000")
    txn.cashin_status = CashInStatus.received
    txn.settlement_status = SettlementStatus.failed
    txn.xrpl_error_reason = "tecPATH_DRY: no trust line"
    await db.commit()
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get(f"/admin/transactions/{txn.id}")

    assert "tecPATH_DRY" in response.text


async def test_monitor_detail_unknown_id_redirects(client: AsyncClient, db: AsyncSession):
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get(f"/admin/transactions/{uuid.uuid4()}", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/admin/transactions"


# --- FR-ADM-06 / FR-FX-08: fee configuration ---

async def test_fee_edit_changes_later_quotes_only(client: AsyncClient, db: AsyncSession, seed_fee_config):
    """A config edit prices new quotes; a past transaction keeps its snapshot."""
    txn, _, _ = await remittance(db, "1000")
    before = {
        "fee": txn.transaction_fee,
        "rate": txn.exchange_rate,
        "uctusd": txn.uctusd_amount,
    }
    quote_before = await fx_service.quote_for(db, Decimal("1000"), PayoutCurrency.USD)
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.post(
            "/admin/config/fees",
            data={
                "fixed_fee_zar": "50.00",
                "percentage_fee": "0.03",
                "fx_margin": "0.05",
                "cashout_fee_percentage": "0.02",
                "cashout_fee_min_usd": "2",
                "market_rate_zar_per_usd": "19.00",
                "min_send_zar": "100.00",
            },
            follow_redirects=False,
        )

    assert response.status_code == 302
    cfg = await fx_service.get_active_fee_config(db)
    assert cfg.fixed_fee_zar == Decimal("50.00")
    assert cfg.fx_margin == Decimal("0.05")
    assert cfg.market_rate_zar_per_usd == Decimal("19.00")

    quote_after = await fx_service.quote_for(db, Decimal("1000"), PayoutCurrency.USD)
    assert quote_after.transaction_fee != quote_before.transaction_fee
    assert quote_after.exchange_rate != quote_before.exchange_rate
    assert quote_after.uctusd_amount != quote_before.uctusd_amount

    await db.refresh(txn)
    assert txn.transaction_fee == before["fee"]
    assert txn.exchange_rate == before["rate"]
    assert txn.uctusd_amount == before["uctusd"]


@pytest.mark.parametrize(
    "bad_field,bad_value",
    [
        ("fixed_fee_zar", "-1"),
        ("percentage_fee", "abc"),
        ("fx_margin", ""),
        ("cashout_fee_min_usd", "-0.5"),
        ("market_rate_zar_per_usd", "0"),
        # AUDIT #2: a minimum at or below the fixed fee would let through a send
        # the fee consumes entirely.
        ("min_send_zar", "25.00"),
        ("min_send_zar", "10.00"),
    ],
)
async def test_fee_edit_rejects_invalid_input(
    client: AsyncClient, db: AsyncSession, seed_fee_config, bad_field, bad_value
):
    admin = await admin_user(db)
    form = {
        "fixed_fee_zar": "25.00",
        "percentage_fee": "0.015",
        "fx_margin": "0.02",
        "cashout_fee_percentage": "0.01",
        "cashout_fee_min_usd": "1",
        "market_rate_zar_per_usd": "18.50",
        "min_send_zar": "50.00",
        bad_field: bad_value,
    }

    with logged_in(admin):
        response = await client.post("/admin/config/fees", data=form, follow_redirects=False)

    assert response.status_code == 302
    cfg = await fx_service.get_active_fee_config(db)
    assert cfg.fixed_fee_zar == Decimal("25.00")  # untouched
    assert cfg.market_rate_zar_per_usd == Decimal("18.50")


async def test_config_page_renders_both_editors(client: AsyncClient, db: AsyncSession, seed_fee_config):
    admin = await admin_user(db)

    with logged_in(admin):
        response = await client.get("/admin/config")

    assert response.status_code == 200
    assert "/admin/config/fees" in response.text     # fee editor
    assert "/admin/config/tiers/" in response.text   # tier editor still there


# --- FR-ADM-07: AML flag ---

async def test_aml_flag_toggles(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db, "1000")
    admin = await admin_user(db)
    assert txn.aml_flagged is False

    with logged_in(admin):
        raised = await client.post(
            f"/admin/transactions/{txn.id}/aml", data={"flagged": "1"}, follow_redirects=False
        )
        await db.refresh(txn)
        assert raised.status_code == 302
        assert txn.aml_flagged is True

        cleared = await client.post(
            f"/admin/transactions/{txn.id}/aml", data={"flagged": "0"}, follow_redirects=False
        )
        await db.refresh(txn)
        assert cleared.status_code == 302
        assert txn.aml_flagged is False


async def test_aml_flag_does_not_move_money_or_status(db: AsyncSession):
    txn, _, _ = await remittance(db, "1000")
    await cashin_service.set_aml_flag(db, txn, True)

    assert txn.aml_flagged is True
    assert txn.cashin_status == CashInStatus.pending
    assert txn.settlement_status == SettlementStatus.not_queued


async def test_monitor_filters_by_aml_flag(client: AsyncClient, db: AsyncSession):
    flagged, flagged_sender, _ = await remittance(db, "1000")
    clean, clean_sender, _ = await remittance(db, "1100")
    await cashin_service.set_aml_flag(db, flagged, True)
    admin = await admin_user(db)

    with logged_in(admin):
        only_flagged = await client.get("/admin/transactions", params={"aml": "1"})
        only_clean = await client.get("/admin/transactions", params={"aml": "0"})

    assert flagged_sender.full_name in only_flagged.text
    assert clean_sender.full_name not in only_flagged.text
    assert clean_sender.full_name in only_clean.text
    assert flagged_sender.full_name not in only_clean.text
