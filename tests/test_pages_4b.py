"""Phase 4b: send flow (Details → Review → Pay → Status), live-quote hooks, History."""
import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import CashInStatus, SettlementStatus
from tests.remit_helpers import approved_sender, beneficiary_for, logged_in, registered_recipient, remittance

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")


def current_step(html: str) -> str:
    """Label of the stepper step marked aria-current="step"."""
    match = re.search(r'<li class="ds-stepper__step[^"]*"[^>]*aria-current="step">.*?<span class="ds-stepper__label">(.*?)</span>', html, re.S)
    return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""


async def _sender_with_recipient(db):
    sender = await approved_sender(db)
    ben = await beneficiary_for(db, sender, await registered_recipient(db))
    return sender, ben


async def test_details_step_keeps_the_form_contract(client: AsyncClient, db: AsyncSession):
    sender, ben = await _sender_with_recipient(db)
    with logged_in(sender):
        page = (await client.get("/send")).text
    assert current_step(page) == "Details"
    assert f'<option value="{ben.id}"' in page                 # perf harness scrapes this
    assert 'action="/send/review"' in page and 'method="get"' in page
    assert 'name="beneficiary_id"' in page and 'name="zar_amount"' in page
    assert 'data-quote-url="/quote"' in page                    # live quote hook
    for field in ["exchange_rate", "transaction_fee", "net_zar_converted", "uctusd_amount",
                  "cashout_fee_estimate", "payout_estimate"]:
        assert f'data-q="{field}"' in page


async def test_review_step_links_back_to_details(client: AsyncClient, db: AsyncSession):
    sender, ben = await _sender_with_recipient(db)
    with logged_in(sender):
        page = (await client.get(f"/send/review?beneficiary_id={ben.id}&zar_amount=1000")).text
    assert current_step(page) == "Review"
    assert f'href="/send?beneficiary_id={ben.id}&amp;zar_amount=1000' in page
    assert "Continue to payment" in page and "50.874404 UCTUSD" in page
    assert "ds-breakdown__row--hero" in page


async def test_pay_step_keeps_the_price_lock(client: AsyncClient, db: AsyncSession):
    sender, ben = await _sender_with_recipient(db)
    with logged_in(sender):
        page = (await client.get(f"/send/pay?beneficiary_id={ben.id}&zar_amount=1000")).text
    assert current_step(page) == "Pay"
    assert 'action="/remittances"' in page
    assert re.search(r'name="exchange_rate" value="[\d.]+"', page)      # perf harness regex
    for name in ["beneficiary_id", "zar_amount", "transaction_fee", "uctusd_amount",
                 "card_name", "card_number", "card_expiry", "card_cvv"]:
        assert f'name="{name}"' in page
    assert "R1,000.00" in page and "Pay R1,000.00" in page


async def test_status_page_for_the_sender(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db)
    with logged_in(sender):
        page = (await client.get(f"/transactions/{txn.id}")).text
    assert current_step(page) == "Status"
    assert "•••• 4242" in page
    assert "Awaiting payment" in page                            # transaction_badge
    assert re.search(r'data-step="cashin"[^>]*>|aria-current="step" data-step="cashin"', page)
    assert f'data-status-poll="/status?t={txn.id}"' in page and 'data-final="false"' in page


async def test_status_page_for_the_recipient_has_no_stepper(client: AsyncClient, db: AsyncSession):
    txn, _, recipient = await remittance(db)
    with logged_in(recipient):
        page = (await client.get(f"/transactions/{txn.id}")).text
    assert "ds-stepper" not in page
    assert "From " in page


async def test_history_uses_the_shared_status_vocabulary(client: AsyncClient, db: AsyncSession):
    txn, sender, _ = await remittance(db)
    txn.cashin_status = CashInStatus.received
    txn.settlement_status = SettlementStatus.completed
    txn.xrpl_tx_hash = "ABCDEF0123456789"
    await db.commit()
    with logged_in(sender):
        page = (await client.get("/transactions")).text
    assert "Settled on XRPL" in page                             # not the raw "Completed"
    assert "https://testnet.xrpl.org/transactions/ABCDEF0123456789" in page
    assert 'data-copy="ABCDEF0123456789"' in page


async def test_amount_field_is_capped_and_spinner_free(client: AsyncClient, db: AsyncSession):
    """Up to 9 digits and 2 decimals; a text field (no native spinner); name/id unchanged."""
    sender, _ = await _sender_with_recipient(db)
    with logged_in(sender):
        page = (await client.get("/send")).text
    field = re.search(r'<input[^>]*id="zar_amount"[^>]*>', page, re.S).group(0)
    assert 'name="zar_amount"' in field and 'type="text"' in field and 'inputmode="decimal"' in field
    assert r'pattern="\d{1,9}(\.\d{1,2})?"' in field and 'maxlength="12"' in field


async def test_top_bar_actions_only_where_needed(client: AsyncClient, db: AsyncSession):
    """History's Send button lives in the sidebar; Beneficiaries keeps Add (no other way)."""
    sender, _ = await _sender_with_recipient(db)
    with logged_in(sender):
        history = (await client.get("/transactions")).text
        beneficiaries = (await client.get("/beneficiaries")).text

    def bar(page):
        start = page.index('<header class="ds-topbar">')
        return page[start:page.index("</header>", start)]

    assert 'href="/send"' not in bar(history)
    assert 'href="/beneficiaries/new"' in bar(beneficiaries)
