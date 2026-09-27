"""Phase 4d: cash-out flow (Amount → Review → Status), timeline, My cash-outs."""
import re
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cashout import CashOutStatus
from app.services import cashout_service
from tests.remit_helpers import logged_in, registered_recipient
from tests.test_cashout import a_request, an_admin, funded_recipient

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")

# The perf harness's own regex (perf/locustfile.py): the price lock's attribute order.
CASHOUT_FIELDS = re.compile(
    r'name="market_rate" value="([\d.]+)".*?name="cashout_fee_usd" value="([\d.]+)".*?name="net_payout" value="([\d.]+)"',
    re.S,
)
HASH = "C" * 64


def current_step(html: str) -> str:
    """Label of the stepper step marked aria-current="step"."""
    match = re.search(r'<li class="ds-stepper__step[^"]*"[^>]*aria-current="step">.*?<span class="ds-stepper__label">(.*?)</span>', html, re.S)
    return re.sub(r"<[^>]+>", "", match.group(1)).strip() if match else ""


def step_state(html: str, key: str) -> str:
    match = re.search(rf'<li class="ds-timeline__step is-(\w+)"[^>]*data-step="{key}"', html)
    return match.group(1) if match else ""


# --- Amount ------------------------------------------------------------------------

async def test_amount_step_keeps_the_form_contract(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db, "100")
    with logged_in(user):
        page = (await client.get("/cashout")).text
    assert current_step(page) == "Amount"
    assert 'action="/cashout/preview"' in page and 'method="post"' in page
    assert 'id="uctusd_amount"' in page and 'name="uctusd_amount"' in page
    assert 'id="target_currency"' in page and 'name="target_currency"' in page
    assert 'pattern="\\d{1,9}(\\.\\d{1,6})?"' in page
    assert 'data-fill-max="100.000000"' in page
    assert "Available" in page and "100.000000 UCTUSD" in page


async def test_amount_step_without_a_wallet(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    user.can_receive = True
    await db.commit()
    with logged_in(user):
        page = (await client.get("/cashout")).text
    assert "No wallet yet" in page and 'id="uctusd_amount"' not in page


async def test_a_refused_amount_returns_to_the_amount_step(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db, "5")
    with logged_in(user):
        r = await client.post("/cashout/preview", data={"uctusd_amount": "50", "target_currency": "ZAR"})
    assert r.status_code == 400
    assert current_step(r.text) == "Amount"
    assert "at most" in r.text and 'value="50"' in r.text
    assert '<option value="ZAR" selected>' in r.text


# --- Review ------------------------------------------------------------------------

async def test_review_step_shows_the_breakdown_and_keeps_the_price_lock(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db, "100")
    with logged_in(user):
        page = (await client.post("/cashout/preview", data={"uctusd_amount": "10", "target_currency": "ZAR"})).text
    assert current_step(page) == "Review"
    assert 'href="/cashout"' in page                                   # Amount links back
    assert CASHOUT_FIELDS.search(page)                                 # perf harness regex
    assert 'action="/cashout"' in page and "Confirm cash-out" in page
    assert "ds-breakdown__row--hero" in page and "R166.50" in page     # (10 - 1) * 18.50
    assert "−$1.00" in page and "1 USD = 18.500000 ZAR" in page
    assert 'data-count-to="166.50' in page
    assert "simulated" in page


async def test_confirming_lands_on_the_status_step(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db, "100")
    with logged_in(user):
        review = (await client.post("/cashout/preview", data={"uctusd_amount": "10", "target_currency": "USD"})).text
        market_rate, fee, net = CASHOUT_FIELDS.search(review).groups()
        r = await client.post("/cashout", data={
            "uctusd_amount": "10", "target_currency": "USD",
            "market_rate": market_rate, "cashout_fee_usd": fee, "net_payout": net,
        }, follow_redirects=False)
    assert r.status_code == 302
    req = (await cashout_service.list_for_recipient(db, user.id))[0]
    assert r.headers["location"] == f"/cashout/{req.id}"


# --- Status ------------------------------------------------------------------------

async def test_status_step_for_a_new_request(client: AsyncClient, db: AsyncSession):
    req, user, _ = await a_request(db, balance="100", amount="10", currency="ZAR")
    with logged_in(user):
        page = (await client.get(f"/cashout/{req.id}")).text
    assert current_step(page) == "Status"
    assert [step_state(page, k) for k in ("requested", "approved", "completed")] == ["done", "current", "upcoming"]
    assert "R166.50" in page and "simulated" in page.lower()
    assert "has not been reserved yet" in page
    assert f'data-status-poll="/status?c={req.id}"' in page and 'data-final="false"' in page
    assert re.search(r'data-failure-box hidden', page)


async def test_status_step_while_the_burn_is_unconfirmed(client: AsyncClient, db: AsyncSession):
    req, user, _ = await a_request(db, balance="100", amount="10")
    req.status, req.approved_at = CashOutStatus.approved, datetime.now(timezone.utc)
    req.xrpl_burn_tx_hash, req.failure_reason = HASH, "outcome_unknown: timeout"
    await db.commit()
    with logged_in(user):
        page = (await client.get(f"/cashout/{req.id}")).text
    assert [step_state(page, k) for k in ("approved", "completed")] == ["done", "current"]
    assert "Awaiting ledger confirmation" in page
    assert f"https://testnet.xrpl.org/transactions/{HASH}" in page
    assert "outcome_unknown" not in page


async def test_status_step_when_completed(client: AsyncClient, db: AsyncSession):
    req, user, _ = await a_request(db, balance="100", amount="10")
    now = datetime.now(timezone.utc)
    req.status, req.approved_at, req.completed_at, req.xrpl_burn_tx_hash = CashOutStatus.completed, now, now, HASH
    req.fiat_payout_reference = "SIM-TEST-1"
    await db.commit()
    with logged_in(user):
        page = (await client.get(f"/cashout/{req.id}")).text
    assert [step_state(page, k) for k in ("requested", "approved", "completed")] == ["done", "done", "done"]
    assert "SIM-TEST-1" in page and 'data-final="true"' in page
    assert re.search(r"<span data-check >", page)          # the check is shown, not hidden


async def test_a_failed_cashout_shows_one_failed_step_and_the_reason(client: AsyncClient, db: AsyncSession):
    """Rejected and failed-after-approval look alike in the data (reject also
    stamps approved_at), so neither claims an approval that may not have happened."""
    rejected, user, _ = await a_request(db, balance="100", amount="10")
    await cashout_service.reject(db, rejected, await an_admin(db), "Details did not match")
    burned, burned_user, _ = await a_request(db, balance="100", amount="10")
    burned.status, burned.approved_at, burned.failure_reason = (
        CashOutStatus.failed, datetime.now(timezone.utc), "tecPATH_DRY")
    await db.commit()

    for req, owner, reason in ((rejected, user, "Details did not match"), (burned, burned_user, "tecPATH_DRY")):
        with logged_in(owner):
            page = (await client.get(f"/cashout/{req.id}")).text
        assert step_state(page, "requested") == "done" and step_state(page, "approved") == "failed"
        assert 'data-step="completed"' not in page and "Cash-out failed" in page
        assert reason in page and "data-failure-box hidden" not in page
        assert f'data-status-sig="failed/false"' in page


# --- My cash-outs ------------------------------------------------------------------

async def test_history_lists_cashouts_as_live_rows(client: AsyncClient, db: AsyncSession):
    req, user, _ = await a_request(db, balance="100", amount="10", currency="ZAR")
    with logged_in(user):
        page = (await client.get("/cashout/history")).text
    assert f'href="/cashout/{req.id}"' in page
    assert "−10.000000 UCTUSD" in page and "R166.50" in page
    assert 'data-status-poll="/status"' in page and f'data-kind="c" data-id="{req.id}"' in page
    assert "simulated" in page


async def test_history_empty_state(client: AsyncClient, db: AsyncSession):
    user, _ = await funded_recipient(db, "100")
    with logged_in(user):
        page = (await client.get("/cashout/history")).text
    assert "No cash-outs yet" in page and "ds-activity__row" not in page
