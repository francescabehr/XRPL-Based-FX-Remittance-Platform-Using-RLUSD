"""FR-WAL-01..04  XRPL service + seed encryption.

xrpl-py's network calls are replaced with fakes — these tests never hit Testnet.
Keys are generated offline, so the real treasury seed is never needed or loaded.
"""
import logging
import uuid
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from xrpl.asyncio.transaction import XRPLReliableSubmissionException
from xrpl.models.transactions import Payment, TrustSet
from xrpl.wallet import Wallet as XRPLWallet

from app.config import settings
from app.models.wallet import Wallet
from app.security import crypto
from app.services import xrpl_service
from app.services.auth_service import create_user

CURRENCY = "5543545553440000000000000000000000000000"
ISSUER = "rELez4x4Zqv3KYqboYVfrYPF8521Ycbxa5"


@pytest.fixture(autouse=True)
def xrpl_settings(monkeypatch):
    """Offline treasury + a fresh Fernet key, independent of the developer's .env."""
    treasury = XRPLWallet.create()
    monkeypatch.setattr(settings, "xrpl_encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "xrpl_currency_code", CURRENCY)
    monkeypatch.setattr(settings, "xrpl_issuer_address", ISSUER)
    monkeypatch.setattr(settings, "xrpl_platform_wallet_address", treasury.classic_address)
    monkeypatch.setattr(settings, "xrpl_platform_wallet_seed", treasury.seed)
    return treasury


class FakeSigned:
    def __init__(self, tx, signer):
        self.tx, self.signer = tx, signer

    def get_hash(self):
        return f"HASH-{self.tx.transaction_type}-{self.tx.account[:6]}"


class FakeResponse:
    def __init__(self, result):
        self.result = result


class FakeLedger:
    """Stands in for autofill_and_sign / submit_and_wait / the faucet, recording every call."""

    def __init__(self):
        self.submitted: list[FakeSigned] = []
        self.outcomes: list[str] = []  # queued result codes; default tesSUCCESS
        self.faucet_accounts: list[XRPLWallet] = []

    async def autofill_and_sign(self, tx, client, signer):
        return FakeSigned(tx, signer)

    async def submit_and_wait(self, signed, client):
        self.submitted.append(signed)
        code = self.outcomes.pop(0) if self.outcomes else "tesSUCCESS"
        if code != "tesSUCCESS":
            raise XRPLReliableSubmissionException(f"Transaction failed: {code}")
        return FakeResponse({"meta": {"TransactionResult": code}, "hash": signed.get_hash()})

    async def generate_faucet_wallet(self, client):
        account = XRPLWallet.create()
        self.faucet_accounts.append(account)
        return account


@pytest.fixture
def ledger(monkeypatch):
    fake = FakeLedger()
    monkeypatch.setattr(xrpl_service, "autofill_and_sign", fake.autofill_and_sign)
    monkeypatch.setattr(xrpl_service, "submit_and_wait", fake.submit_and_wait)
    monkeypatch.setattr(xrpl_service, "generate_faucet_wallet", fake.generate_faucet_wallet)
    return fake


async def _recipient(db):
    n = uuid.uuid4().hex[:8]
    return await create_user(
        db, full_name=f"Recipient {n}", email=f"rcp{n}@test.com",
        mobile=f"+2771{int(n, 16) % 10**7:07d}", password="Pass1234!",
    )


# --- crypto (FR-WAL-03) ---

def test_seed_encryption_round_trip():
    seed = XRPLWallet.create().seed
    token = crypto.encrypt_seed(seed)

    assert token != seed and seed not in token
    assert crypto.decrypt_seed(token) == seed


def test_key_id_is_stable_and_not_the_key():
    assert crypto.key_id() == crypto.key_id()
    assert crypto.key_id() not in settings.xrpl_encryption_key


def test_decrypt_with_wrong_key_fails_without_leaking(monkeypatch):
    token = crypto.encrypt_seed("sEdSECRETSEEDVALUE")
    monkeypatch.setattr(settings, "xrpl_encryption_key", Fernet.generate_key().decode())

    with pytest.raises(crypto.KeyConfigError) as exc:
        crypto.decrypt_seed(token)
    assert token not in str(exc.value)


def test_invalid_key_is_a_config_error(monkeypatch):
    monkeypatch.setattr(settings, "xrpl_encryption_key", "not-a-fernet-key")
    with pytest.raises(crypto.KeyConfigError):
        crypto.encrypt_seed("anything")


# --- amounts + treasury config ---

def test_uctusd_uses_configured_currency_and_6dp():
    amount = xrpl_service.uctusd(Decimal("50.8744044"))

    assert amount.currency == CURRENCY
    assert amount.issuer == ISSUER
    assert amount.value == "50.874404"


def test_uctusd_currency_is_config_not_constant(monkeypatch):
    monkeypatch.setattr(settings, "xrpl_currency_code", "USD")
    assert xrpl_service.uctusd(Decimal("1")).currency == "USD"


@pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("0.0000001")])
def test_uctusd_rejects_non_positive(bad):
    with pytest.raises(ValueError):
        xrpl_service.uctusd(bad)


def test_load_treasury_matches_address(xrpl_settings):
    assert xrpl_service.load_treasury().classic_address == xrpl_settings.classic_address


def test_load_treasury_rejects_mismatched_address(monkeypatch):
    monkeypatch.setattr(settings, "xrpl_platform_wallet_address", XRPLWallet.create().classic_address)
    with pytest.raises(xrpl_service.XRPLConfigError):
        xrpl_service.load_treasury()


def test_load_treasury_invalid_seed_is_not_echoed(monkeypatch):
    monkeypatch.setattr(settings, "xrpl_platform_wallet_seed", "sBOGUS-SEED-VALUE")
    with pytest.raises(xrpl_service.XRPLConfigError) as exc:
        xrpl_service.load_treasury()
    assert "sBOGUS" not in str(exc.value)
    assert exc.value.__cause__ is None


# --- submission + failure codes (FR-WAL-06/07) ---

async def test_send_from_treasury_success(ledger, xrpl_settings):
    dest = XRPLWallet.create().classic_address
    result = await xrpl_service.send_from_treasury(dest, Decimal("10"), client=object())

    assert result.success and result.result_code == "tesSUCCESS"
    signed = ledger.submitted[0]
    assert isinstance(signed.tx, Payment)
    assert signed.tx.account == xrpl_settings.classic_address
    assert signed.tx.destination == dest
    assert signed.tx.amount.value == "10.000000"
    assert signed.signer.classic_address == xrpl_settings.classic_address
    assert result.tx_hash == signed.get_hash()


@pytest.mark.parametrize("code", ["tecPATH_DRY", "tecPATH_PARTIAL"])
async def test_ledger_failure_returns_code_and_hash(ledger, code):
    ledger.outcomes.append(code)
    result = await xrpl_service.send_from_treasury(
        XRPLWallet.create().classic_address, Decimal("1"), client=object()
    )

    assert not result.success
    assert result.result_code == code
    assert result.tx_hash is not None  # signed before submit, so the failed tx is traceable


async def test_sign_failure_is_reported_not_raised(monkeypatch):
    from xrpl.constants import XRPLException

    async def boom(tx, client, signer):
        raise XRPLException("autofill failed")

    monkeypatch.setattr(xrpl_service, "autofill_and_sign", boom)
    result = await xrpl_service.send_from_treasury(
        XRPLWallet.create().classic_address, Decimal("1"), client=object()
    )
    assert (result.success, result.result_code, result.tx_hash) == (False, "sign_error", None)


# --- provisioning (FR-WAL-01..04) ---

async def test_provision_wallet_creates_encrypted_trustlined_wallet(db, ledger):
    user = await _recipient(db)
    wallet = await xrpl_service.provision_wallet(db, user, client=object())

    account = ledger.faucet_accounts[0]
    assert wallet.user_id == user.id
    assert wallet.xrpl_address == account.classic_address
    assert account.seed not in wallet.encrypted_private_key
    assert crypto.decrypt_seed(wallet.encrypted_private_key) == account.seed
    assert wallet.key_encryption_key_id == crypto.key_id()
    assert wallet.trust_set_complete is True

    trust = ledger.submitted[0].tx
    assert isinstance(trust, TrustSet)
    assert trust.account == wallet.xrpl_address
    assert trust.limit_amount.currency == CURRENCY
    assert trust.limit_amount.issuer == ISSUER


async def test_provision_wallet_is_idempotent(db, ledger):
    user = await _recipient(db)
    first = await xrpl_service.provision_wallet(db, user, client=object())
    second = await xrpl_service.provision_wallet(db, user, client=object())

    assert first.id == second.id
    assert len(ledger.faucet_accounts) == 1
    assert len(ledger.submitted) == 1  # no second TrustSet


async def test_failed_trustset_is_retried_on_next_provision(db, ledger):
    user = await _recipient(db)
    ledger.outcomes.append("tecNO_LINE_INSUF_RESERVE")

    wallet = await xrpl_service.provision_wallet(db, user, client=object())
    assert wallet.trust_set_complete is False

    wallet = await xrpl_service.provision_wallet(db, user, client=object())
    assert wallet.trust_set_complete is True
    assert len(ledger.faucet_accounts) == 1


async def test_burn_goes_to_issuer_signed_by_recipient(db, ledger):
    user = await _recipient(db)
    wallet = await xrpl_service.provision_wallet(db, user, client=object())

    result = await xrpl_service.burn_to_issuer(wallet, Decimal("5"), client=object())

    burn = ledger.submitted[-1]
    assert result.success
    assert burn.tx.account == wallet.xrpl_address
    assert burn.tx.destination == ISSUER
    assert burn.signer.classic_address == wallet.xrpl_address


async def test_seeds_never_logged(db, ledger, caplog, xrpl_settings):
    caplog.set_level(logging.DEBUG)
    user = await _recipient(db)
    ledger.outcomes.append("tecPATH_DRY")

    wallet = await xrpl_service.provision_wallet(db, user, client=object())
    await xrpl_service.send_from_treasury(wallet.xrpl_address, Decimal("1"), client=object())

    assert ledger.faucet_accounts[0].seed not in caplog.text
    assert xrpl_settings.seed not in caplog.text


def test_wallet_repr_omits_key_material():
    w = Wallet(xrpl_address="rTEST", encrypted_private_key="gAAAA-secret-token", user_id=uuid.uuid4())
    assert "secret-token" not in repr(w)


# --- balance read ---

class FakeLinesClient:
    def __init__(self, lines):
        self.lines = lines

    async def request(self, req):
        return FakeResponse({"lines": self.lines})


async def test_balance_reads_matching_trust_line():
    client = FakeLinesClient([
        {"currency": "USD", "account": ISSUER, "balance": "3"},
        {"currency": CURRENCY, "account": ISSUER, "balance": "12.5"},
    ])
    assert await xrpl_service.get_uctusd_balance("rX", client=client) == Decimal("12.5")


async def test_balance_none_without_trust_line():
    assert await xrpl_service.get_uctusd_balance("rX", client=FakeLinesClient([])) is None


# --- Phase 6 additions: hash-before-submit hook + ledger lookup (FR-WAL-06, FR-MQ-06) ---

async def test_on_signed_receives_hash_before_submission(ledger, monkeypatch):
    events = []

    async def tracking_submit(signed, client):
        events.append("submitted")
        return await ledger.submit_and_wait(signed, client)

    monkeypatch.setattr(xrpl_service, "submit_and_wait", tracking_submit)

    async def on_signed(tx_hash):
        events.append(("signed", tx_hash))

    result = await xrpl_service.send_from_treasury(
        XRPLWallet.create().classic_address, Decimal("1"), client=object(), on_signed=on_signed
    )

    assert events == [("signed", result.tx_hash), "submitted"]


async def test_on_signed_also_fires_when_ledger_rejects(ledger):
    seen = []
    ledger.outcomes.append("tecPATH_DRY")

    async def on_signed(tx_hash):
        seen.append(tx_hash)

    result = await xrpl_service.send_from_treasury(
        XRPLWallet.create().classic_address, Decimal("1"), client=object(), on_signed=on_signed
    )
    assert not result.success and seen == [result.tx_hash]


async def test_on_signed_not_called_when_signing_fails(monkeypatch):
    from xrpl.constants import XRPLException

    async def boom(tx, client, signer):
        raise XRPLException("autofill failed")

    monkeypatch.setattr(xrpl_service, "autofill_and_sign", boom)
    seen = []

    async def on_signed(tx_hash):
        seen.append(tx_hash)

    result = await xrpl_service.send_from_treasury(
        XRPLWallet.create().classic_address, Decimal("1"), client=object(), on_signed=on_signed
    )
    assert result.result_code == "sign_error" and seen == []


class FakeTxClient:
    """Answers the Tx lookup used by get_transaction_result."""

    def __init__(self, ok, result):
        self.ok, self.result, self.requests = ok, result, []

    async def request(self, req):
        self.requests.append(req)
        ok, result = self.ok, self.result

        class Resp:
            def __init__(self):
                self.result = result

            def is_successful(self):
                return ok

        return Resp()


async def test_transaction_result_for_validated_tx():
    client = FakeTxClient(True, {"validated": True, "meta": {"TransactionResult": "tesSUCCESS"}})
    assert await xrpl_service.get_transaction_result("ABC", client=client) == "tesSUCCESS"
    assert client.requests[0].transaction == "ABC"


async def test_transaction_result_for_validated_failure():
    client = FakeTxClient(True, {"validated": True, "meta": {"TransactionResult": "tecPATH_DRY"}})
    assert await xrpl_service.get_transaction_result("ABC", client=client) == "tecPATH_DRY"


@pytest.mark.parametrize(
    "ok, result",
    [
        (True, {"validated": False, "meta": {"TransactionResult": "tesSUCCESS"}}),  # not final yet
        (False, {"error": "txnNotFound"}),  # never seen / expired
    ],
)
async def test_transaction_result_none_until_final(ok, result):
    assert await xrpl_service.get_transaction_result("ABC", client=FakeTxClient(ok, result)) is None


async def test_provisioning_marks_user_as_receiver(db, ledger):
    user = await _recipient(db)
    assert user.can_receive is False
    await xrpl_service.provision_wallet(db, user, client=object())
    await db.refresh(user)
    assert user.can_receive is True
