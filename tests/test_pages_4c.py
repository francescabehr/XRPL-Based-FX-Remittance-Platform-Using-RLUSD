"""Phase 4c: GET /status (live polling), the merged wallet list and its drawer."""
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.beneficiary import PayoutCurrency
from app.models.cashout import CashOutStatus
from app.models.transaction import CashInStatus, SettlementStatus
from app.services import cashout_service, xrpl_service
from tests.remit_helpers import logged_in, remittance
from tests.test_cashout import an_admin, funded_recipient

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")

HASH = "A" * 64


async def _status(client, **params):
    query = "&".join(f"{k}={v}" for k, values in params.items() for v in values)
    return await client.get(f"/status?{query}")


async def _cashout(db, balance="100", amount="10"):
    user, wallet = await funded_recipient(db, balance)
    req = await cashout_service.create_request(
        db, user, amount=Decimal(amount), currency=PayoutCurrency("USD")
    )
    return req, user, wallet


# --- /status: access -----------------------------------------------------------

async def test_status_requires_sign_in(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db)
    r = await _status(client, t=[txn.id])
    assert r.status_code == 401


async def test_transaction_visible_to_sender_recipient_and_admin(client: AsyncClient, db: AsyncSession):
    txn, sender, recipient = await remittance(db)
    for user in (sender, recipient, await an_admin(db)):
        with logged_in(user):
            body = (await _status(client, t=[txn.id])).json()
        assert str(txn.id) in body["transactions"]


async def test_transaction_omitted_for_anyone_else(client: AsyncClient, db: AsyncSession):
    txn, _, _ = await remittance(db)
    _, stranger, _ = await remittance(db)
    with logged_in(stranger):
        r = await _status(client, t=[txn.id])
    assert r.status_code == 200
    assert r.json()["transactions"] == {}


async def test_cashout_visible_to_owner_and_admin_only(client: AsyncClient, db: AsyncSession):
    req, owner, _ = await _cashout(db)
    other, _ = await funded_recipient(db)
    with logged_in(owner):
        assert str(req.id) in (await _status(client, c=[req.id])).json()["cashouts"]
    with logged_in(await an_admin(db)):
        assert str(req.id) in (await _status(client, c=[req.id])).json()["cashouts"]
    with logged_in(other):
        assert (await _status(client, c=[req.id])).json()["cashouts"] == {}


async def test_invalid_ids_are_ignored(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db)
    with logged_in(sender):
        r = await _status(client, t=["nope", txn.id], c=["123"])
    assert r.status_code == 200
    body = r.json()
    assert list(body["transactions"]) == [str(txn.id)] and body["cashouts"] == {}


async def test_at_most_fifty_ids_are_read(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db)
    unknown = [uuid.uuid4() for _ in range(60)]
    with logged_in(sender):
        late = (await _status(client, t=unknown[:50] + [txn.id])).json()
        early = (await _status(client, t=[txn.id] + unknown)).json()
    assert late["transactions"] == {}
    assert str(txn.id) in early["transactions"]


# --- /status: final flags and rendered markup ------------------------------------

@pytest.mark.parametrize(
    "cashin, settlement, final",
    [
        (CashInStatus.pending, SettlementStatus.not_queued, False),
        (CashInStatus.failed, SettlementStatus.not_queued, True),
        (CashInStatus.received, SettlementStatus.queued, False),
        (CashInStatus.received, SettlementStatus.processing, False),
        (CashInStatus.received, SettlementStatus.completed, True),
        (CashInStatus.received, SettlementStatus.failed, True),
    ],
)
async def test_transaction_final_flag(client: AsyncClient, db: AsyncSession, cashin, settlement, final):
    txn, sender, _ = await remittance(db)
    txn.cashin_status, txn.settlement_status = cashin, settlement
    await db.commit()
    with logged_in(sender):
        body = (await _status(client, t=[txn.id])).json()
    item = body["transactions"][str(txn.id)]
    assert item["final"] is final and body["all_final"] is final
    assert item["cashin_status"] == cashin.value and item["settlement_status"] == settlement.value


async def test_settled_transaction_renders_badge_and_explorer_link(client: AsyncClient, db: AsyncSession):
    txn, _, recipient = await remittance(db)
    txn.cashin_status, txn.settlement_status, txn.xrpl_tx_hash = CashInStatus.received, SettlementStatus.completed, HASH
    await db.commit()
    with logged_in(recipient):
        item = (await _status(client, t=[txn.id])).json()["transactions"][str(txn.id)]
    assert "Settled on XRPL" in item["badge_html"] and "ds-status--success" in item["badge_html"]
    assert item["hash"] == HASH
    assert f"https://testnet.xrpl.org/transactions/{HASH}" in item["hash_html"]


@pytest.mark.parametrize(
    "status, burn_hash, reason, final, label",
    [
        (CashOutStatus.requested, None, None, False, "Requested"),
        (CashOutStatus.approved, None, None, False, "Approved"),
        (CashOutStatus.approved, HASH, "outcome_unknown: timeout", False, "Awaiting ledger confirmation"),
        (CashOutStatus.completed, HASH, None, True, "Completed"),
        (CashOutStatus.failed, None, "Burn rejected", True, "Failed"),
    ],
)
async def test_cashout_final_flag(client: AsyncClient, db: AsyncSession, status, burn_hash, reason, final, label):
    req, owner, _ = await _cashout(db)
    req.status, req.xrpl_burn_tx_hash, req.failure_reason = status, burn_hash, reason
    await db.commit()
    with logged_in(owner):
        body = (await _status(client, c=[req.id])).json()
    item = body["cashouts"][str(req.id)]
    assert item["final"] is final and body["all_final"] is final
    assert label in item["badge_html"]
    if label == "Awaiting ledger confirmation":
        assert item["awaiting_ledger"] is True and item["failure_reason"] is None
    if status == CashOutStatus.failed:
        assert item["failure_reason"] == "Burn rejected"


async def test_wallet_balance_for_recipient_not_admin(client: AsyncClient, db: AsyncSession):
    req, owner, wallet = await _cashout(db, balance="50.874404", amount="10")
    with logged_in(owner):
        assert (await _status(client, c=[req.id])).json()["wallet_balance"] == "50.874404"
    with logged_in(await an_admin(db)):
        assert (await _status(client, c=[req.id])).json()["wallet_balance"] is None


# --- wallet page -----------------------------------------------------------------

async def test_wallet_merges_transfers_and_cashouts(client: AsyncClient, db: AsyncSession, monkeypatch):
    async def no_ledger(address, client=None):
        raise ConnectionError("offline")

    monkeypatch.setattr(xrpl_service, "get_uctusd_balance", no_ledger)

    user, wallet = await funded_recipient(db, "100")
    txn, _, _ = await remittance(db)
    txn.recipient_user_id = user.id
    txn.cashin_status, txn.settlement_status, txn.xrpl_tx_hash = CashInStatus.received, SettlementStatus.completed, HASH
    await db.commit()
    req = await cashout_service.create_request(db, user, amount=Decimal("10"), currency=PayoutCurrency("USD"))

    with logged_in(user):
        page = (await client.get("/wallet")).text

    # Newest first: the cash-out was made after the transfer.
    out_row, in_row = page.find(f'href="/cashout/{req.id}"'), page.find(f'href="/transactions/{txn.id}"')
    assert -1 < out_row < in_row
    assert "+50.874404 UCTUSD" in page and "−10.000000 UCTUSD" in page
    assert page.count('data-drawer-open="wallet-drawer"') == 2
    assert f'<template id="wd-t-{txn.id}">' in page and f'<template id="wd-c-{req.id}">' in page
    assert 'id="wallet-drawer"' in page and 'data-status-poll="/status"' in page
    assert "Ledger balance unavailable" in page and "50.874404" in page
    assert f"https://testnet.xrpl.org/transactions/{HASH}" in page
    # A requested cash-out is still in flight; the settled transfer is final.
    assert f'data-kind="c" data-id="{req.id}"' in page
    assert f'data-id="{txn.id}" data-hash-value="{HASH}" data-final="true"' in page
    assert f'data-id="{req.id}" data-hash-value="" data-final="false"' in page


async def test_wallet_empty_state(client: AsyncClient, db: AsyncSession, monkeypatch):
    async def no_ledger(address, client=None):
        raise ConnectionError("offline")

    monkeypatch.setattr(xrpl_service, "get_uctusd_balance", no_ledger)
    user, _ = await funded_recipient(db, "0")
    with logged_in(user):
        page = (await client.get("/wallet")).text
    assert "No activity yet" in page and "ds-activity__row" not in page


async def test_cashout_detail_carries_the_poll_hook(client: AsyncClient, db: AsyncSession):
    req, owner, _ = await _cashout(db)
    with logged_in(owner):
        page = (await client.get(f"/cashout/{req.id}")).text
    assert f'data-status-poll="/status?c={req.id}"' in page and 'data-final="false"' in page
    assert "data-badge" in page and "data-hash" in page
