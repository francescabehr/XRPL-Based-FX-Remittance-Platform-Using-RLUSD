"""Shared builders and fakes for the cash-in / settlement tests (not a test module)."""
import uuid
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import select

from app.dependencies import get_current_user
from app.main import app
from app.models.user import KYCStatus
from app.models.wallet import Wallet
from app.services.auth_service import create_user
from app.services.beneficiary_service import create_beneficiary
from app.services.cashin_service import AcceptedQuote, MockCard, create_remittance
from app.services.fx_service import quote_for
from app.services import xrpl_service
from app.services.xrpl_service import XRPLResult

GOOD_CARD = MockCard("4242 4242 4242 4242", "12/30", "123", "Test Sender")
DECLINED_CARD = MockCard("4000 0000 0000 0002", "12/30", "123", "Test Sender")


def _tag() -> str:
    return uuid.uuid4().hex[:8]


def _mobile() -> str:
    return f"+2779{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}"


async def approved_sender(db, **kwargs):
    t = _tag()
    user = await create_user(
        db, full_name=f"Sender {t}", email=f"snd{t}@test.com", mobile=_mobile(), password="Pass1234!", **kwargs
    )
    user.kyc_status = KYCStatus.approved
    await db.commit()
    return user


async def registered_recipient(db):
    t = _tag()
    return await create_user(
        db, full_name=f"Recipient {t}", email=f"rcv{t}@test.com", mobile=_mobile(), password="Pass1234!"
    )


async def beneficiary_for(db, sender, recipient=None, *, email=None, currency="USD"):
    return await create_beneficiary(
        db, sender,
        full_name=recipient.full_name if recipient else "Unregistered Person",
        email=recipient.email if recipient else (email or f"nobody{_tag()}@test.com"),
        mobile=None, country="Zimbabwe", payout_currency=currency, relationship="Sibling",
    )


async def quoted(db, ben, amount) -> AcceptedQuote:
    """The price lock a sender's browser posts back: the quote they were shown.

    Every create_remittance call must carry one — the server re-prices and refuses
    if it no longer matches (requirements.md §382).
    """
    q = await quote_for(db, Decimal(amount), ben.payout_currency)
    return AcceptedQuote(
        exchange_rate=q.exchange_rate,
        transaction_fee=q.transaction_fee,
        uctusd_amount=q.uctusd_amount,
    )


async def remittance(db, amount="1000", card=GOOD_CARD):
    """An approved sender -> registered recipient remittance with a pending cash-in."""
    sender = await approved_sender(db)
    recipient = await registered_recipient(db)
    ben = await beneficiary_for(db, sender, recipient)
    txn = await create_remittance(
        db, sender, beneficiary_id=ben.id, zar_amount=Decimal(amount), card=card,
        accepted=await quoted(db, ben, amount),
    )
    return txn, sender, recipient


class RecordingEnqueue:
    """Stands in for queue_service.enqueue_settlement."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def __call__(self, idempotency_key, transaction_id):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append((idempotency_key, transaction_id))
        return "job-1"


class RecordingRequeue:
    """Stands in for queue_service.enqueue_settlement_in."""

    def __init__(self):
        self.calls: list[tuple[int, str, str]] = []

    def __call__(self, seconds, idempotency_key, transaction_id):
        self.calls.append((seconds, idempotency_key, transaction_id))
        return "job-2"


class FakeXRPL:
    """Replaces xrpl_service.provision_wallet / send_from_treasury — no Testnet."""

    def __init__(self):
        self.provisioned = 0
        self.payments: list[tuple[str, Decimal]] = []
        # "tesSUCCESS" | "tec..." (validated failure) | "sign_error" | Exception
        # | ("raise_after_sign", exc)
        self.outcomes: list = []
        self.trust_ok = True
        self.delay = 0.0
        # The ledger range a signed payment could land in (mirrors FakeBurn).
        self.last_ledger_sequence = 500
        self.submitted_ledger_index = 480

    async def provision_wallet(self, db, user, client=None):
        self.provisioned += 1
        wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one_or_none()
        if wallet is None:
            wallet = Wallet(
                id=uuid.uuid4(), user_id=user.id, xrpl_address=f"r{uuid.uuid4().hex[:24]}",
                encrypted_private_key="token", key_encryption_key_id="kid",
            )
            user.can_receive = True
            db.add(wallet)
        wallet.trust_set_complete = self.trust_ok
        await db.commit()
        return wallet

    async def send_from_treasury(
        self, destination, amount, client=None, on_signed=None, on_signed_tx=None
    ):
        import asyncio

        outcome = self.outcomes.pop(0) if self.outcomes else "tesSUCCESS"
        if isinstance(outcome, Exception):
            raise outcome  # fails before signing
        if outcome == "sign_error":
            return XRPLResult(False, "sign_error", None, "autofill failed")

        tx_hash = f"HASH{len(self.payments):04d}{uuid.uuid4().hex[:8]}".upper()
        if on_signed:
            await on_signed(tx_hash)
        if on_signed_tx:
            await on_signed_tx(
                xrpl_service.SignedTx(
                    tx_hash=tx_hash,
                    last_ledger_sequence=self.last_ledger_sequence,
                    submitted_ledger_index=self.submitted_ledger_index,
                )
            )
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(outcome, tuple):
            raise outcome[1]  # fails after signing
        self.payments.append((destination, Decimal(amount)))
        if outcome == "tesSUCCESS":
            return XRPLResult(True, outcome, tx_hash, outcome=xrpl_service.SUCCEEDED)
        # A validated tec*: the ledger proved the payment moved nothing.
        return XRPLResult(
            False, outcome, tx_hash, f"Transaction failed: {outcome}",
            outcome=xrpl_service.FAILED,
        )


@contextmanager
def logged_in(user):
    async def _override():
        return user

    app.dependency_overrides[get_current_user] = _override
    try:
        yield
    finally:
        app.dependency_overrides.pop(get_current_user, None)
