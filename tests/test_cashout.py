"""
FR-CO-01..06  Cash-out: pricing, reservation, on-chain burn, reversal, reconcile.

xrpl-py and Redis are faked throughout — no live Testnet. The concurrency and
row-lock tests run against the real PostgreSQL test database, using separate
sessions so the locks actually contend.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.beneficiary import PayoutCurrency
from app.models.cashout import CashOutRequest, CashOutStatus
from app.models.wallet import Wallet
from app.services import cashout_service, fx_service, queue_service, xrpl_service
from app.services.cashout_service import CashOutError, PricingChanged
from app.workers import cashout_worker
from app.workers.cashout_worker import MAX_ATTEMPTS, process_burn
from tests.conftest import _TestSession
from tests.remit_helpers import RecordingRequeue, logged_in, registered_recipient

pytestmark = pytest.mark.usefixtures("seed_tiers", "seed_fee_config")

SUCCESS = "tesSUCCESS"


# --- fakes -------------------------------------------------------------------

class RecordingBurnEnqueue:
    """Stands in for queue_service.enqueue_burn."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, idempotency_key, cashout_id):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append((idempotency_key, cashout_id))
        return "burn-job-1"


class FakeBurn:
    """Replaces xrpl_service.burn_to_issuer — no Testnet.

    outcomes entries: "tesSUCCESS" | "tec..." | "sign_error" | Exception
                    | ("raise_after_sign", exc) | ("no_result", msg)
    """

    def __init__(self):
        self.burns: list[tuple[str, Decimal]] = []
        self.outcomes: list = []
        self.delay = 0.0
        self.last_ledger_sequence = 500
        self.submitted_ledger_index = 480
        self.persist_error: Exception | None = None

    async def __call__(self, wallet, amount, client=None, on_signed_tx=None):
        outcome = self.outcomes.pop(0) if self.outcomes else SUCCESS
        if isinstance(outcome, Exception):
            raise outcome  # before signing — nothing was ever built

        if outcome == "sign_error":
            return xrpl_service.XRPLResult(False, "sign_error", None, "autofill failed")

        tx_hash = f"BURN{len(self.burns):04d}{uuid.uuid4().hex[:8]}".upper()
        if on_signed_tx is not None:
            if self.persist_error is not None:
                raise self.persist_error  # persistence failed => never submitted
            await on_signed_tx(
                xrpl_service.SignedTx(
                    tx_hash=tx_hash,
                    last_ledger_sequence=self.last_ledger_sequence,
                    submitted_ledger_index=self.submitted_ledger_index,
                )
            )
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(outcome, tuple) and outcome[0] == "raise_after_sign":
            raise outcome[1]
        if isinstance(outcome, tuple) and outcome[0] == "no_result":
            return xrpl_service.XRPLResult(False, "submission_error", tx_hash, outcome[1])

        self.burns.append((wallet.xrpl_address, Decimal(amount)))
        if outcome == SUCCESS:
            return xrpl_service.XRPLResult(True, SUCCESS, tx_hash)
        return xrpl_service.XRPLResult(False, outcome, tx_hash, f"Transaction failed: {outcome}")


@pytest.fixture
def burner(monkeypatch):
    fake = FakeBurn()
    monkeypatch.setattr(xrpl_service, "burn_to_issuer", fake)
    return fake


# --- builders ----------------------------------------------------------------

async def funded_recipient(db, balance="100"):
    """A recipient holding a trust-lined wallet with a cached UCTUSD balance."""
    user = await registered_recipient(db)
    user.can_receive = True
    wallet = Wallet(
        id=uuid.uuid4(),
        user_id=user.id,
        xrpl_address=f"r{uuid.uuid4().hex[:24]}",
        encrypted_private_key="token",
        key_encryption_key_id="kid",
        trust_set_complete=True,
        balance_uctusd=Decimal(balance),
    )
    db.add_all([user, wallet])
    await db.commit()
    await db.refresh(wallet)
    return user, wallet


async def a_request(db, balance="100", amount="10", currency="USD"):
    user, wallet = await funded_recipient(db, balance)
    req = await cashout_service.create_request(
        db, user, amount=Decimal(amount), currency=PayoutCurrency(currency)
    )
    return req, user, wallet


async def an_admin(db):
    from app.services.auth_service import create_user

    t = uuid.uuid4().hex[:8]
    admin = await create_user(
        db, full_name=f"Admin {t}", email=f"adm{t}@test.com",
        mobile=f"+2778{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}", password="Pass1234!",
    )
    admin.is_admin = True
    await db.commit()
    return admin


async def approved(db, **kwargs):
    """A requested cash-out that an admin has approved (so it is debited + queued)."""
    req, user, wallet = await a_request(db, **kwargs)
    admin = await an_admin(db)
    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())
    return req, user, wallet


async def _requests_for(db, user):
    """This user's cash-outs only — the test database is shared across tests."""
    return list((await db.execute(
        select(CashOutRequest).where(CashOutRequest.recipient_user_id == user.id)
    )).scalars().all())


async def _fresh(req_id):
    async with _TestSession() as s:
        return await s.get(CashOutRequest, req_id)


async def _balance(wallet_id):
    async with _TestSession() as s:
        w = await s.get(Wallet, wallet_id)
        return None if w is None else Decimal(w.balance_uctusd)


def _burn(req, requeue=None):
    return process_burn(req.idempotency_key, _TestSession, requeue=requeue or RecordingRequeue())


# --- FR-CO-02  payout math ---------------------------------------------------

def test_usd_payout_is_amount_less_fee():
    fee = fx_service.cashout_fee_usd(Decimal("50"), Decimal("0.01"), Decimal("1"))
    assert fee == Decimal("1.000000")  # 1% of 50 == 0.50, floored at the $1 minimum
    payout = fx_service.cashout_payout(Decimal("50"), fee, Decimal("18.50"), PayoutCurrency.USD)
    assert payout == Decimal("49.000000")


def test_zar_payout_converts_after_the_fee():
    fee = fx_service.cashout_fee_usd(Decimal("200"), Decimal("0.01"), Decimal("1"))
    assert fee == Decimal("2.000000")  # 1% of 200 beats the minimum
    payout = fx_service.cashout_payout(Decimal("200"), fee, Decimal("18.50"), PayoutCurrency.ZAR)
    assert payout == Decimal("3663.00")  # (200 - 2) * 18.50, quantised to 2 dp


def test_percentage_fee_beats_the_minimum_when_larger():
    assert fx_service.cashout_fee_usd(Decimal("500"), Decimal("0.01"), Decimal("1")) == Decimal("5.000000")


def test_zar_payout_rounds_to_two_places():
    fee = Decimal("1.000000")
    payout = fx_service.cashout_payout(Decimal("10.333333"), fee, Decimal("18.505555"), PayoutCurrency.ZAR)
    # (10.333333 - 1) * 18.505555 == 172.718507164815; ROUND_HALF_UP to 2 dp
    assert payout == Decimal("172.72")
    assert payout.as_tuple().exponent == -2


def test_quote_and_cashout_agree_on_the_same_figures():
    """The extracted helpers are the same code the Phase 4 quote uses (no drift)."""
    quote = fx_service.calculate_quote(
        zar_send=Decimal("1000"), market_rate=Decimal("18.50"), fixed_fee_zar=Decimal("25"),
        percentage_fee=Decimal("0.015"), fx_margin=Decimal("0.02"),
        cashout_fee_percentage=Decimal("0.01"), cashout_fee_min_usd=Decimal("1"),
        payout_currency=PayoutCurrency.ZAR,
    )
    assert quote.uctusd_amount == Decimal("50.874404")  # Phase 4 worked example, unchanged
    fee = fx_service.cashout_fee_usd(quote.uctusd_amount, Decimal("0.01"), Decimal("1"))
    assert quote.cashout_fee_estimate == fee
    assert quote.payout_estimate == fx_service.cashout_payout(
        quote.uctusd_amount, fee, Decimal("18.50"), PayoutCurrency.ZAR
    )


async def test_non_positive_payout_is_rejected(db):
    user, _ = await funded_recipient(db, "100")
    with pytest.raises(CashOutError, match="too small"):
        # $1 minimum fee against a $0.50 cash-out leaves nothing to pay out.
        await cashout_service.create_request(
            db, user, amount=Decimal("0.5"), currency=PayoutCurrency.USD
        )


def test_only_usd_and_zar_are_accepted():
    assert cashout_service.parse_currency("usd") == PayoutCurrency.USD
    assert cashout_service.parse_currency("ZAR") == PayoutCurrency.ZAR
    for bad in ("GBP", "EUR", "", "XRP"):
        with pytest.raises(CashOutError, match="USD or ZAR"):
            cashout_service.parse_currency(bad)


def test_amounts_must_be_positive_numbers():
    assert cashout_service.parse_amount("10.1234567") == Decimal("10.123457")
    for bad in ("0", "-5", "abc", ""):
        with pytest.raises(CashOutError):
            cashout_service.parse_amount(bad)


# --- FR-CO-01  request + available balance -----------------------------------

async def test_request_is_created_with_pricing_snapshot(db):
    req, _, _ = await a_request(db, balance="100", amount="10", currency="ZAR")

    assert req.status == CashOutStatus.requested
    assert req.uctusd_amount == Decimal("10.000000")
    assert req.market_rate == Decimal("18.500000")
    assert req.cashout_fee_usd == Decimal("1.000000")      # 1% of 10 floored at $1
    assert req.net_payout == Decimal("166.50")             # (10 - 1) * 18.50
    assert req.idempotency_key is not None


async def test_over_balance_request_is_rejected(db):
    user, _ = await funded_recipient(db, "10")
    with pytest.raises(CashOutError, match="at most"):
        await cashout_service.create_request(
            db, user, amount=Decimal("10.000001"), currency=PayoutCurrency.USD
        )
    assert await _requests_for(db, user) == []


async def test_requested_rows_hold_balance_but_approved_ones_do_not(db):
    """Approved amounts already left balance_uctusd; counting them again would double-subtract."""
    req, user, wallet = await a_request(db, balance="100", amount="40")
    assert await cashout_service.available_balance(db, user.id) == Decimal("60")

    admin = await an_admin(db)
    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())

    # The debit moved the 40 out of the balance; availability must not drop by 80.
    assert await _balance(wallet.id) == Decimal("60")
    assert await cashout_service.held_for_requests(db, user.id) == Decimal("0")
    assert await cashout_service.available_balance(db, user.id) == Decimal("60")


async def test_second_request_cannot_exceed_what_the_first_left(db):
    _, user, _ = await a_request(db, balance="100", amount="70")
    with pytest.raises(CashOutError, match="at most"):
        await cashout_service.create_request(
            db, user, amount=Decimal("40"), currency=PayoutCurrency.USD
        )


async def test_concurrent_requests_cannot_oversubscribe_the_wallet(db):
    """Real Postgres row lock: two simultaneous requests, only one can fit."""
    user, wallet = await funded_recipient(db, "100")

    async def request(amount):
        async with _TestSession() as s:
            fresh = await s.get(type(user), user.id)
            try:
                await cashout_service.create_request(
                    s, fresh, amount=Decimal(amount), currency=PayoutCurrency.USD
                )
                return "created"
            except CashOutError:
                return "rejected"

    results = await asyncio.gather(request("60"), request("60"))

    assert sorted(results) == ["created", "rejected"]
    async with _TestSession() as s:
        rows = (await s.execute(
            select(CashOutRequest).where(CashOutRequest.recipient_user_id == user.id)
        )).scalars().all()
    assert len(rows) == 1
    assert await cashout_service.available_balance(db, user.id) == Decimal("40")


async def test_stale_pricing_requires_a_fresh_preview(db):
    user, _ = await funded_recipient(db, "100")
    stale = cashout_service.CashOutPricing(
        uctusd_amount=Decimal("10"), target_currency=PayoutCurrency.USD,
        market_rate=Decimal("17.000000"),  # the rate has since moved
        cashout_fee_percentage=Decimal("0"), cashout_fee_min_usd=Decimal("0"),
        cashout_fee_usd=Decimal("1.000000"), net_payout=Decimal("9.000000"),
    )
    with pytest.raises(PricingChanged):
        await cashout_service.create_request(
            db, user, amount=Decimal("10"), currency=PayoutCurrency.USD, accepted=stale
        )


async def test_client_supplied_payout_is_never_persisted(db):
    """A tampered payout must be refused, not written."""
    user, _ = await funded_recipient(db, "100")
    tampered = cashout_service.CashOutPricing(
        uctusd_amount=Decimal("10"), target_currency=PayoutCurrency.USD,
        market_rate=Decimal("18.500000"), cashout_fee_percentage=Decimal("0"),
        cashout_fee_min_usd=Decimal("0"), cashout_fee_usd=Decimal("0.000000"),
        net_payout=Decimal("9999.000000"),
    )
    with pytest.raises(PricingChanged):
        await cashout_service.create_request(
            db, user, amount=Decimal("10"), currency=PayoutCurrency.USD, accepted=tampered
        )
    assert await _requests_for(db, user) == []


# --- FR-CO-03/05  approval, forward-only status ------------------------------

async def test_approval_debits_once_and_is_attributed(db):
    req, _, wallet = await a_request(db, balance="100", amount="25")
    admin = await an_admin(db)
    enqueue = RecordingBurnEnqueue()

    await cashout_service.approve(db, req, admin, enqueue=enqueue)

    assert req.status == CashOutStatus.approved
    assert req.approved_by == admin.id and req.approved_at is not None
    assert await _balance(wallet.id) == Decimal("75")
    assert enqueue.calls == [(str(req.idempotency_key), str(req.id))]


async def test_approval_uses_saved_pricing_not_a_recompute(db):
    """A fee change after the request must not alter what was agreed (FR-CO-02)."""
    req, _, _ = await a_request(db, balance="100", amount="20", currency="ZAR")
    agreed_rate, agreed_payout = req.market_rate, req.net_payout

    cfg = await fx_service.get_active_fee_config(db)
    cfg.market_rate_zar_per_usd = Decimal("25.00")
    cfg.cashout_fee_percentage = Decimal("0.25")
    await db.commit()

    admin = await an_admin(db)
    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())

    stored = await _fresh(req.id)
    assert stored.market_rate == agreed_rate
    assert stored.net_payout == agreed_payout


async def test_duplicate_approval_debits_once(db):
    req, _, wallet = await a_request(db, balance="100", amount="30")
    admin = await an_admin(db)

    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())
    with pytest.raises(CashOutError, match="already been actioned"):
        await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())

    assert await _balance(wallet.id) == Decimal("70")


async def test_concurrent_approvals_debit_once(db):
    req, _, wallet = await a_request(db, balance="100", amount="30")
    admin = await an_admin(db)

    async def do_approve():
        async with _TestSession() as s:
            fresh = await s.get(CashOutRequest, req.id)
            adm = await s.get(type(admin), admin.id)
            try:
                await cashout_service.approve(s, fresh, adm, enqueue=RecordingBurnEnqueue())
                return "approved"
            except CashOutError:
                return "refused"

    results = await asyncio.gather(do_approve(), do_approve(), do_approve())

    assert sorted(results) == ["approved", "refused", "refused"]
    assert await _balance(wallet.id) == Decimal("70")


async def test_approval_refused_when_balance_no_longer_covers_it(db):
    """Balance can be spent between request and approval; approval must not go negative."""
    req, _, wallet = await a_request(db, balance="100", amount="80")
    wallet.balance_uctusd = Decimal("10")
    await db.commit()

    admin = await an_admin(db)
    with pytest.raises(CashOutError, match="not enough"):
        await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())

    stored = await _fresh(req.id)
    assert stored.status == CashOutStatus.requested
    assert await _balance(wallet.id) == Decimal("10")


async def test_status_cannot_skip_or_reverse(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    await _burn(req)
    assert (await _fresh(req.id)).status == CashOutStatus.completed

    async with _TestSession() as s:
        done = await s.get(CashOutRequest, req.id)
        admin = await an_admin(db)
        # completed is terminal: no re-approval, no reconcile, no going back.
        with pytest.raises(CashOutError):
            await cashout_service.approve(s, done, admin, enqueue=RecordingBurnEnqueue())
        with pytest.raises(CashOutError, match="already resolved"):
            await cashout_service.reconcile(s, done)
        assert not await cashout_worker.fail_and_restore(s, done, "should not apply")

    assert (await _fresh(req.id)).status == CashOutStatus.completed
    assert await _balance(wallet.id) == Decimal("90")  # never restored


async def test_reject_before_approval_never_debits(db):
    req, _, wallet = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)

    await cashout_service.reject(db, req, admin, "Documents required")

    assert req.status == CashOutStatus.failed
    assert req.failure_reason == "Documents required"
    assert await _balance(wallet.id) == Decimal("100")


# --- FR-CO-05  burn happy path ----------------------------------------------

async def test_successful_burn_completes_once_with_payout(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")

    assert await _burn(req) == "completed"

    done = await _fresh(req.id)
    assert done.status == CashOutStatus.completed
    assert done.xrpl_burn_tx_hash and done.completed_at
    assert done.fiat_payout_reference and done.fiat_paid_at
    assert done.failure_reason is None
    # Debited at approval, not again at completion.
    assert await _balance(wallet.id) == Decimal("90")
    # The FULL amount is burned; the fee only reduces the simulated fiat payout.
    assert burner.burns == [(wallet.xrpl_address, Decimal("10.000000"))]
    assert done.net_payout == Decimal("9.000000")


async def test_burn_persists_recovery_metadata_before_submitting(db, burner):
    req, _, _ = await approved(db, balance="100", amount="10")
    await _burn(req)

    done = await _fresh(req.id)
    assert done.burn_last_ledger_sequence == 500
    assert done.burn_submitted_ledger_index == 480


async def test_persistence_failure_means_nothing_is_submitted(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.persist_error = RuntimeError("db write failed")

    assert await _burn(req) == "requeued"

    held = await _fresh(req.id)
    assert held.xrpl_burn_tx_hash is None
    assert burner.burns == []           # nothing was ever submitted
    assert held.burn_started_at is None  # claim released for a clean retry
    assert await _balance(wallet.id) == Decimal("90")


# --- FR-CO-06  idempotency ---------------------------------------------------

async def test_redelivered_burn_message_is_a_no_op(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")

    assert await _burn(req) == "completed"
    assert await _burn(req) == "skipped"

    assert len(burner.burns) == 1
    assert await _balance(wallet.id) == Decimal("90")


async def test_concurrent_deliveries_burn_once(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.delay = 0.05  # keep the first burn in flight while the others arrive

    results = await asyncio.gather(_burn(req), _burn(req), _burn(req))

    assert sorted(results) == ["completed", "skipped", "skipped"]
    assert len(burner.burns) == 1
    assert await _balance(wallet.id) == Decimal("90")


async def test_unapproved_request_is_never_burned(db, burner):
    req, _, _ = await a_request(db, balance="100", amount="10")  # still `requested`
    assert await _burn(req) == "skipped"
    assert burner.burns == []


# --- FR-CO-06  reversal ------------------------------------------------------

async def test_definitive_failure_restores_the_balance_once(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.outcomes = ["tecPATH_DRY"]

    assert await _burn(req) == "failed"

    failed = await _fresh(req.id)
    assert failed.status == CashOutStatus.failed
    assert "tecPATH_DRY" in failed.failure_reason
    assert failed.xrpl_burn_tx_hash  # kept for the audit trail
    assert await _balance(wallet.id) == Decimal("100")  # reserve returned

    # A redelivery must not restore a second time.
    assert await _burn(req) == "skipped"
    assert await _balance(wallet.id) == Decimal("100")


async def test_failure_restore_never_pushes_the_balance_negative(db, burner):
    req, _, wallet = await approved(db, balance="10", amount="10")
    assert await _balance(wallet.id) == Decimal("0")

    burner.outcomes = ["tecUNFUNDED_PAYMENT"]
    assert await _burn(req) == "failed"

    assert await _balance(wallet.id) == Decimal("10")
    assert await _balance(wallet.id) >= 0


async def test_retries_exhausted_restores_the_balance(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.outcomes = ["sign_error"] * (MAX_ATTEMPTS + 1)
    requeue = RecordingRequeue()

    outcomes = [await _burn(req, requeue) for _ in range(MAX_ATTEMPTS)]

    assert outcomes[:-1] == ["requeued"] * (MAX_ATTEMPTS - 1)
    assert outcomes[-1] == "failed"
    assert burner.burns == []
    assert await _balance(wallet.id) == Decimal("100")
    assert len(requeue.calls) == MAX_ATTEMPTS - 1


async def test_queue_publish_failure_restores_the_reserve(db):
    req, _, wallet = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)

    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue(fail=True))

    failed = await _fresh(req.id)
    assert failed.status == CashOutStatus.failed
    assert "queue_unavailable" in failed.failure_reason
    assert await _balance(wallet.id) == Decimal("100")


# --- unknown outcome: hold the reserve --------------------------------------

async def test_unknown_outcome_keeps_the_debit_and_holds(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.outcomes = [("raise_after_sign", TimeoutError("ledger timeout"))]

    assert await _burn(req) == "unknown"

    held = await _fresh(req.id)
    assert held.status == CashOutStatus.approved          # not failed
    assert held.failure_reason.startswith("outcome_unknown")
    assert held.awaiting_ledger_confirmation
    assert held.status_label == "Awaiting ledger confirmation"
    assert held.xrpl_burn_tx_hash
    assert await _balance(wallet.id) == Decimal("90")     # still debited


async def test_a_held_burn_is_never_auto_resubmitted(db, burner):
    req, _, _ = await approved(db, balance="100", amount="10")
    burner.outcomes = [("raise_after_sign", TimeoutError("ledger timeout"))]
    await _burn(req)

    # Any redelivery of the message must bounce off the claim: it has a hash.
    assert await _burn(req) == "skipped"
    assert await _burn(req) == "skipped"
    assert burner.burns == []


async def test_missing_validated_result_is_treated_as_unknown(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.outcomes = [("no_result", "no validated result returned")]

    assert await _burn(req) == "unknown"

    held = await _fresh(req.id)
    assert held.status == CashOutStatus.approved
    assert await _balance(wallet.id) == Decimal("90")


# --- reconcile: ledger proof, never the clock -------------------------------

async def _held(db, burner, balance="100", amount="10"):
    req, _, wallet = await approved(db, balance=balance, amount=amount)
    burner.outcomes = [("raise_after_sign", TimeoutError("ledger timeout"))]
    await _burn(req)
    return await _fresh(req.id), wallet


async def test_reconcile_completes_a_burn_that_succeeded(db, burner):
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        outcome = await cashout_service.reconcile(s, held, ledger_result=_result(SUCCESS))
    assert outcome == "completed"

    done = await _fresh(req.id)
    assert done.status == CashOutStatus.completed
    assert done.fiat_payout_reference
    assert await _balance(wallet.id) == Decimal("90")  # burned: never restored


async def test_reconcile_fails_and_restores_on_a_validated_failure(db, burner):
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        outcome = await cashout_service.reconcile(s, held, ledger_result=_result("tecPATH_DRY"))
    assert outcome == "failed"
    assert await _balance(wallet.id) == Decimal("100")


async def test_reconcile_restores_once_when_called_repeatedly(db, burner):
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        assert await cashout_service.reconcile(s, held, ledger_result=_result("tecPATH_DRY")) == "failed"

    async with _TestSession() as s:
        again = await s.get(CashOutRequest, req.id)
        with pytest.raises(CashOutError, match="already resolved"):
            await cashout_service.reconcile(s, again, ledger_result=_result("tecPATH_DRY"))

    assert await _balance(wallet.id) == Decimal("100")  # restored exactly once


async def test_reconcile_completes_once_when_called_repeatedly(db, burner):
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        assert await cashout_service.reconcile(s, held, ledger_result=_result(SUCCESS)) == "completed"
    async with _TestSession() as s:
        again = await s.get(CashOutRequest, req.id)
        with pytest.raises(CashOutError, match="already resolved"):
            await cashout_service.reconcile(s, again, ledger_result=_result(SUCCESS))

    assert await _balance(wallet.id) == Decimal("90")


async def test_tx_not_found_stays_unresolved_while_it_could_still_land(db, burner):
    """Before LastLedgerSequence passes, absence proves nothing."""
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        outcome = await cashout_service.reconcile(
            s, held,
            ledger_result=_result(None),
            latest_ledger=_ledger(499),   # still <= last_ledger_sequence (500)
            ledger_range=_range(True),
        )
    assert outcome == "unresolved"

    still_held = await _fresh(req.id)
    assert still_held.status == CashOutStatus.approved
    assert await _balance(wallet.id) == Decimal("90")  # reserve stays held


async def test_tx_not_found_stays_unresolved_when_history_has_gaps(db, burner):
    """Past expiry, but the server cannot see the whole window — still not proof."""
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        outcome = await cashout_service.reconcile(
            s, held,
            ledger_result=_result(None),
            latest_ledger=_ledger(900),   # well past expiry
            ledger_range=_range(False),   # but history is incomplete
        )
    assert outcome == "unresolved"
    assert await _balance(wallet.id) == Decimal("90")


async def test_proven_expiry_fails_and_restores(db, burner):
    """Past LastLedgerSequence AND full history coverage: the burn can never land."""
    req, wallet = await _held(db, burner)

    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        outcome = await cashout_service.reconcile(
            s, held, ledger_result=_result(None),
            latest_ledger=_ledger(900), ledger_range=_range(True),
        )
    assert outcome == "failed"

    failed = await _fresh(req.id)
    assert "expired" in failed.failure_reason
    assert await _balance(wallet.id) == Decimal("100")


async def test_reconcile_without_recovery_metadata_stays_unresolved(db, burner):
    req, wallet = await _held(db, burner)
    async with _TestSession() as s:
        held = await s.get(CashOutRequest, req.id)
        held.burn_last_ledger_sequence = None
        await s.commit()
        outcome = await cashout_service.reconcile(
            s, held, ledger_result=_result(None),
            latest_ledger=_ledger(900), ledger_range=_range(True),
        )
    assert outcome == "unresolved"
    assert await _balance(wallet.id) == Decimal("90")


async def test_reconcile_refuses_a_request_with_no_submitted_burn(db):
    req, _, _ = await approved(db, balance="100", amount="10")
    with pytest.raises(CashOutError, match="no submitted burn"):
        await cashout_service.reconcile(db, req)


def _result(code):
    async def _f(tx_hash, client=None):
        return code
    return _f


def _ledger(index):
    async def _f(client=None):
        return index
    return _f


def _range(ok):
    async def _f(start, end, client=None):
        return ok
    return _f


def test_complete_ledgers_parsing_requires_unbroken_coverage():
    parse = xrpl_service._parse_complete_ledgers
    assert parse("32570-97531234") == [(32570, 97531234)]
    assert parse("100-200,300-400") == [(100, 200), (300, 400)]
    assert parse("empty") == []
    assert parse("") == []
    # A range split across a gap must not be treated as covering the whole window.
    assert not any(low <= 150 and 350 <= high for low, high in parse("100-200,300-400"))


# --- worker-crash recovery + the commit-before-publish sweep -----------------

async def test_claim_expires_for_a_worker_that_died_before_signing(db, burner):
    req, _, wallet = await approved(db, balance="100", amount="10")

    # Simulate a worker that claimed the row and then died without signing.
    async with _TestSession() as s:
        stale = await s.get(CashOutRequest, req.id)
        stale.burn_started_at = datetime.now(timezone.utc) - cashout_worker.STUCK_AFTER - timedelta(minutes=1)
        await s.commit()

    assert await _burn(req) == "completed"
    assert len(burner.burns) == 1
    assert await _balance(wallet.id) == Decimal("90")


async def test_a_stale_claim_does_not_permit_a_second_signing(db, burner):
    """Crash AFTER signing: the hash blocks re-claim no matter how old the claim is."""
    req, _, wallet = await approved(db, balance="100", amount="10")
    burner.outcomes = [("raise_after_sign", TimeoutError("died after signing"))]
    await _burn(req)

    async with _TestSession() as s:
        stale = await s.get(CashOutRequest, req.id)
        stale.burn_started_at = datetime.now(timezone.utc) - cashout_worker.STUCK_AFTER - timedelta(hours=1)
        await s.commit()

    assert await _burn(req) == "skipped"   # never re-signed
    assert burner.burns == []
    assert await _balance(wallet.id) == Decimal("90")


async def test_sweep_republishes_a_burn_whose_message_was_lost(db):
    """The commit-before-publish window: approved + debited, but no message exists."""
    req, _, _ = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)
    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue(fail=False))

    # Age the approval past the grace period.
    async with _TestSession() as s:
        aged = await s.get(CashOutRequest, req.id)
        aged.approved_at = datetime.now(timezone.utc) - cashout_service.PUBLISH_GRACE - timedelta(minutes=1)
        await s.commit()

    enqueue = RecordingBurnEnqueue()
    revived = await cashout_service.sweep_unpublished(db, enqueue=enqueue)

    assert req.id in revived
    assert enqueue.calls == [(str(req.idempotency_key), str(req.id))]


async def test_sweep_ignores_fresh_claimed_and_terminal_rows(db, burner):
    enqueue = RecordingBurnEnqueue()

    # Fresh approval: still inside the grace window, the original publish may land.
    fresh, _, _ = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)
    await cashout_service.approve(db, fresh, admin, enqueue=RecordingBurnEnqueue())

    # Completed: nothing to publish.
    done, _, _ = await approved(db, balance="100", amount="10")
    await _burn(done)
    async with _TestSession() as s:
        aged = await s.get(CashOutRequest, done.id)
        aged.approved_at = datetime.now(timezone.utc) - cashout_service.PUBLISH_GRACE - timedelta(minutes=5)
        await s.commit()

    # The sweep is global, so assert about these rows rather than the whole table.
    swept = await cashout_service.sweep_unpublished(db, enqueue=enqueue)
    assert fresh.id not in swept   # inside the grace window
    assert done.id not in swept    # already completed


async def test_sweep_is_safe_to_run_twice(db, burner):
    req, _, wallet = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)
    await cashout_service.approve(db, req, admin, enqueue=RecordingBurnEnqueue())
    async with _TestSession() as s:
        aged = await s.get(CashOutRequest, req.id)
        aged.approved_at = datetime.now(timezone.utc) - cashout_service.PUBLISH_GRACE - timedelta(minutes=1)
        await s.commit()

    enqueue = RecordingBurnEnqueue()
    await cashout_service.sweep_unpublished(db, enqueue=enqueue)
    await cashout_service.sweep_unpublished(db, enqueue=enqueue)
    mine = [c for c in enqueue.calls if c[0] == str(req.idempotency_key)]
    assert len(mine) == 2  # duplicate messages are fine...

    # ...because the claim still burns exactly once.
    assert await _burn(req) == "completed"
    assert await _burn(req) == "skipped"
    assert len(burner.burns) == 1
    assert await _balance(wallet.id) == Decimal("90")


# --- authorization + ownership ----------------------------------------------

async def test_recipient_cannot_see_another_recipients_cashout(db):
    req, _, _ = await a_request(db, balance="100", amount="10")
    other, _ = await funded_recipient(db, "50")

    assert await cashout_service.get_request(db, req.id, recipient_id=other.id) is None
    assert await cashout_service.get_request(db, req.id) is not None  # admin view


async def test_recipient_cashout_detail_is_scoped_to_the_owner(db, client):
    req, _, _ = await a_request(db, balance="100", amount="10")
    other, _ = await funded_recipient(db, "50")

    with logged_in(other):
        response = await client.get(f"/cashout/{req.id}", follow_redirects=False)
    assert response.status_code == 302
    assert "/cashout/history" in response.headers["location"]


async def test_admin_cashout_routes_require_admin(db, client):
    req, user, _ = await a_request(db, balance="100", amount="10")

    with logged_in(user):
        assert (await client.get("/admin/cashout", follow_redirects=False)).status_code == 403
        assert (await client.post(
            f"/admin/cashout/{req.id}/approve", follow_redirects=False
        )).status_code == 403
        assert (await client.post(
            f"/admin/cashout/{req.id}/reconcile", follow_redirects=False
        )).status_code == 403

    assert (await _fresh(req.id)).status == CashOutStatus.requested


async def test_admin_can_work_the_cashout_queue(db, client):
    req, _, wallet = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)

    with logged_in(admin):
        listing = await client.get("/admin/cashout")
        assert listing.status_code == 200
        assert "Cash-out queue" in listing.text


async def test_recipient_can_reach_the_cashout_screens(db, client):
    user, _ = await funded_recipient(db, "100")

    with logged_in(user):
        form = await client.get("/cashout")
        assert form.status_code == 200
        assert "Available" in form.text

        preview = await client.post(
            "/cashout/preview", data={"uctusd_amount": "10", "target_currency": "ZAR"}
        )
        assert preview.status_code == 200
        assert "166.50" in preview.text  # (10 - 1) * 18.50

        history = await client.get("/cashout/history")
        assert history.status_code == 200


async def test_preview_rejects_an_over_balance_amount(db, client):
    user, _ = await funded_recipient(db, "5")

    with logged_in(user):
        response = await client.post(
            "/cashout/preview", data={"uctusd_amount": "50", "target_currency": "USD"}
        )
    assert response.status_code == 400
    assert "at most" in response.text


async def test_detail_page_renders_for_the_owner(db, client):
    req, user, _ = await a_request(db, balance="100", amount="10", currency="ZAR")

    with logged_in(user):
        response = await client.get(f"/cashout/{req.id}")
    assert response.status_code == 200
    assert "166.50" in response.text          # the agreed payout
    assert "simulated" in response.text.lower()  # payouts are labelled simulated


async def test_held_cashout_shows_awaiting_ledger_confirmation(db, client, burner):
    req, user, _ = await approved(db, balance="100", amount="10")
    burner.outcomes = [("raise_after_sign", TimeoutError("ledger timeout"))]
    await _burn(req)

    with logged_in(user):
        response = await client.get(f"/cashout/{req.id}")
    assert response.status_code == 200
    assert "Awaiting ledger confirmation" in response.text


async def test_admin_can_approve_through_the_route(db, client, monkeypatch):
    req, _, wallet = await a_request(db, balance="100", amount="10")
    admin = await an_admin(db)
    enqueue = RecordingBurnEnqueue()
    monkeypatch.setattr(queue_service, "enqueue_burn", enqueue)

    with logged_in(admin):
        response = await client.post(
            f"/admin/cashout/{req.id}/approve", follow_redirects=False
        )
    assert response.status_code == 302

    approved_row = await _fresh(req.id)
    assert approved_row.status == CashOutStatus.approved
    assert approved_row.approved_by == admin.id
    assert await _balance(wallet.id) == Decimal("90")
    assert enqueue.calls == [(str(req.idempotency_key), str(req.id))]
