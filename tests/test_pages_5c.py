"""UI redesign Phase 5c: full transfer history (filters) and the admin transaction monitor."""
import re
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import CashInStatus, SettlementStatus
from app.services.cashin_service import create_remittance
from tests.test_public_pages import undecorated_icons
from tests.remit_helpers import (
    GOOD_CARD, approved_sender, beneficiary_for, logged_in, quoted, registered_recipient, remittance,
)
from tests.test_shell import an_admin


async def three_transfers(db: AsyncSession):
    """One sender with a pending, a settled and a failed transfer."""
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    txns = []
    for _ in range(3):
        txns.append(await create_remittance(
            db, sender, beneficiary_id=ben.id, zar_amount=Decimal("500"), card=GOOD_CARD,
            accepted=await quoted(db, ben, "500"),
        ))
    pending, settled, failed = txns
    settled.cashin_status = CashInStatus.received
    settled.settlement_status = SettlementStatus.completed
    settled.xrpl_tx_hash = "C0FFEE0123456789"
    failed.cashin_status = CashInStatus.received
    failed.settlement_status = SettlementStatus.failed
    await db.commit()
    return sender, pending, settled, failed


def row_ids(page: str) -> set[str]:
    return set(re.findall(r'<a href="/transactions/([0-9a-f-]{36})"', page))


# ── Sender history ────────────────────────────────────────────────────────────

async def test_history_filters(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    sender, pending, settled, failed = await three_transfers(db)
    with logged_in(sender):
        pages = {s: (await client.get("/transactions", params={"status": s} if s else None)).text
                 for s in ("", "in_progress", "settled", "failed")}

    assert row_ids(pages[""]) == {str(pending.id), str(settled.id), str(failed.id)}
    assert row_ids(pages["in_progress"]) == {str(pending.id)}
    assert row_ids(pages["settled"]) == {str(settled.id)}
    assert row_ids(pages["failed"]) == {str(failed.id)}

    everything = pages[""]
    for label in ("All", "In progress", "Settled", "Failed"):
        assert re.search(rf'{label} <span class="ds-filter__count">\d+</span>', everything)
    assert re.search(r'class="nav-link active" href="/transactions"\s*aria-current="page"', everything)
    assert re.search(r'class="nav-link active" href="/transactions\?status=failed"\s*aria-current="page"', pages["failed"])
    # The settled row still links its hash to the explorer, with copy.
    assert "https://testnet.xrpl.org/transactions/C0FFEE0123456789" in pages["settled"]
    assert 'data-copy="C0FFEE0123456789"' in pages["settled"]
    for page in pages.values():
        assert page.count("<h1") == 1 and not undecorated_icons(page)


async def test_history_unknown_filter_shows_everything(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    sender, *txns = await three_transfers(db)
    with logged_in(sender):
        page = (await client.get("/transactions", params={"status": "banana"})).text
    assert row_ids(page) == {str(t.id) for t in txns}


async def test_history_empty_filter_offers_show_all(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    txn, sender, _ = await remittance(db)  # one pending transfer, nothing settled
    with logged_in(sender):
        page = (await client.get("/transactions", params={"status": "settled"})).text
    assert "No settled transfers" in page and 'href="/transactions"' in page
    assert row_ids(page) == set()


async def test_history_empty_state_without_transfers(client: AsyncClient, db: AsyncSession):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/transactions")).text
    assert "No transfers yet" in page and "ds-filter" not in page


# ── Admin monitor ─────────────────────────────────────────────────────────────

async def test_monitor_filter_form_contract(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/transactions")).text
    assert re.search(r'<form method="get" action="/admin/transactions"', page)
    for name, field_id in [("cashin", "f-cashin"), ("settlement", "f-settlement"), ("date_from", "f-date_from"),
                           ("date_to", "f-date_to"), ("aml", "f-aml"), ("q", "f-q")]:
        assert f'name="{name}"' in page and f'id="{field_id}"' in page and f'for="{field_id}"' in page
    for value in [s.value for s in CashInStatus] + [s.value for s in SettlementStatus] + ["1", "0"]:
        assert f'<option value="{value}"' in page
    # Option text uses the shared status vocabulary.
    assert "Settling on XRPL…</option>" in page and "Awaiting payment</option>" in page
    assert "active</span>" not in page  # no filters applied → no count, no Clear
    assert page.count("<h1") == 1 and not undecorated_icons(page)


async def test_monitor_shows_active_filters_and_clear(client: AsyncClient, db: AsyncSession):
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/transactions", params={"aml": "1", "q": "nobody-matches-this"})).text
    assert "2 active</span>" in page
    assert "No transactions match these filters" in page
    assert page.count('href="/admin/transactions"') >= 2  # Clear button + empty-state action
    assert 'value="nobody-matches-this"' in page


async def test_monitor_count_and_detail_link(client: AsyncClient, db: AsyncSession, seed_tiers, seed_fee_config):
    txn, sender, _ = await remittance(db)
    with logged_in(await an_admin(db)):
        page = (await client.get("/admin/transactions", params={"q": sender.email})).text
    assert "1 shown</span>" in page
    assert f'href="/admin/transactions/{txn.id}"' in page
