"""
FR-WAL-01..04  XRPL surface: recipient provisioning, TrustSet, UCTUSD payments, burn.

Every call here was first proven against Testnet by scripts/xrpl_smoke.py — keep
them in step. Rules this module enforces:

- The currency code, issuer and treasury all come from settings, never constants.
- Seeds are decrypted only here (via security/crypto.py), only at signing time,
  and never logged, returned, or stored in plaintext (FR-WAL-03/04).
- Transactions are signed before submission, so the hash is known even when the
  ledger rejects them. Failures come back as an XRPLResult carrying both the
  result code (e.g. tecPATH_DRY) and a resolution saying whether the outcome is
  actually known, never as a raised exception (FR-WAL-07).

Result codes observed on Testnet (Phase 5 smoke run):
  tecPATH_DRY      payment to an account with no UCTUSD trust line
  tecPATH_PARTIAL  payment exceeding the sender's UCTUSD balance
  invalid currency code -> rejected client-side by xrpl-py (XRPLModelException)
"""
from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Awaitable, Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from xrpl.asyncio.clients import AsyncJsonRpcClient
from xrpl.asyncio.ledger import get_latest_validated_ledger_sequence
from xrpl.asyncio.transaction import (
    XRPLReliableSubmissionException,
    autofill_and_sign,
    submit_and_wait,
)
from xrpl.asyncio.wallet import generate_faucet_wallet
from xrpl.constants import XRPLException
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountLines, ServerInfo, Tx
from xrpl.models.transactions import Payment, TrustSet
from xrpl.models.transactions.transaction import Transaction as XRPLTransaction
from xrpl.wallet import Wallet as XRPLWallet

from app.config import settings
from app.models.user import User
from app.models.wallet import Wallet
from app.security import crypto

logger = logging.getLogger(__name__)

QUANT_UCTUSD = Decimal("0.000001")
TRUST_LINE_LIMIT = "1000000000"
SUCCESS = "tesSUCCESS"

# How much the ledger actually told us. Every decision that moves, restores or
# re-sends money branches on XRPLResult.resolution — never on result_code.
#
# A code lifted out of an exception message can be the *preliminary* result of a
# transaction whose real outcome nobody has seen: xrpl-py's reliable submission
# raises "...Prelim result: tesSUCCESS" the moment LastLedgerSequence is passed,
# and does so without a final Tx lookup, so the payment may well be in a
# validated ledger. Treating that as a failure would reverse a burn that already
# destroyed tokens. ter* codes (terQUEUED, terPRE_SEQ) arrive by the same route
# and are just as unproven.
SUCCEEDED = "succeeded"          # validated tesSUCCESS — the funds moved
FAILED = "failed"                # provably did not move: validated tec*, or malformed tem*
UNKNOWN = "unknown"              # signed and sent, outcome never observed — never reverse
NOT_SUBMITTED = "not_submitted"  # nothing was signed or sent — safe to retry

# "Transaction failed: tecPATH_DRY". xrpl-py raises this only after seeing
# result["validated"] is true, so the code it carries is final.
# Pinned to xrpl-py 4.0.0's _wait_for_final_transaction_outcome wording
_VALIDATED_FAILURE = re.compile(r"^Transaction failed: ([a-z]{3}[A-Z][A-Z_]*)$")
# "temBAD_FEE: Invalid fee...". The server rejected the blob outright; a tem*
# transaction is malformed and can never be included in any ledger.
_MALFORMED = re.compile(r"^(tem[A-Z_]+):")

# Called with the tx hash after signing and before submission, so a caller can
# persist the hash first — if the process dies mid-submit, the payment can still
# be traced on the ledger instead of blindly re-sent.
OnSigned = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class SignedTx:
    """What a caller needs to recover a transaction whose outcome it never saw.

    last_ledger_sequence and submitted_ledger_index bracket the only ledger range
    in which this hash can ever appear. Without both, a missing transaction is
    merely absent, not proven dead — see
    https://xrpl.org/docs/concepts/transactions/reliable-transaction-submission
    """

    tx_hash: str
    last_ledger_sequence: Optional[int]
    submitted_ledger_index: Optional[int]


# Richer variant of OnSigned, carrying the recovery metadata. If it raises, the
# transaction is never submitted — persistence failure must not leave an
# unrecorded payment on the ledger.
OnSignedTx = Callable[[SignedTx], Awaitable[None]]


class XRPLConfigError(RuntimeError):
    """Treasury seed/address or other XRPL settings are missing or inconsistent."""


class InvalidAmountError(ValueError):
    """The amount cannot be expressed as UCTUSD, so no transaction can carry it.

    A permanent property of the value, not a transient condition: a worker that
    sees this must fail the job outright, because retrying re-reads the same
    amount and fails identically.
    """


@dataclass(frozen=True)
class XRPLResult:
    """Outcome of one submitted transaction. tx_hash is set whenever signing succeeded.

    result_code is for humans and logs. Callers decide what to do from
    `resolution`, which is one of SUCCEEDED / FAILED / UNKNOWN / NOT_SUBMITTED.
    Left unset, it is inferred conservatively: a failure that was signed counts
    as UNKNOWN, because a transaction with a hash may be on the ledger.
    """

    success: bool
    result_code: str
    tx_hash: Optional[str]
    message: str = ""
    outcome: Optional[str] = None

    @property
    def resolution(self) -> str:
        if self.outcome is not None:
            return self.outcome
        if self.success:
            return SUCCEEDED
        return NOT_SUBMITTED if self.tx_hash is None else UNKNOWN


def get_client() -> AsyncJsonRpcClient:
    return AsyncJsonRpcClient(settings.xrpl_json_rpc)


def uctusd(value: Decimal) -> IssuedCurrencyAmount:
    """A UCTUSD amount using the ledger-verified currency code (6 dp, positive)."""
    try:
        value = Decimal(value).quantize(QUANT_UCTUSD, rounding=ROUND_HALF_UP)
    except (TypeError, ArithmeticError) as exc:
        raise InvalidAmountError(f"Not a usable UCTUSD amount: {value!r}") from exc
    if value <= 0:
        raise InvalidAmountError("UCTUSD amount must be positive.")
    return IssuedCurrencyAmount(
        currency=settings.xrpl_currency_code,
        issuer=settings.xrpl_issuer_address,
        value=format(value, "f"),
    )


def load_treasury() -> XRPLWallet:
    """The platform treasury signer. Refuses to run if the seed doesn't match the address."""
    if not settings.xrpl_platform_wallet_seed or not settings.xrpl_platform_wallet_address:
        raise XRPLConfigError("Treasury wallet address/seed are not configured.")
    try:
        wallet = XRPLWallet.from_seed(settings.xrpl_platform_wallet_seed)
    except Exception:  # noqa: BLE001 — never echo the seed in the error
        raise XRPLConfigError("XRPL_PLATFORM_WALLET_SEED is not a valid seed.") from None
    if wallet.classic_address != settings.xrpl_platform_wallet_address:
        raise XRPLConfigError("Treasury seed does not derive XRPL_PLATFORM_WALLET_ADDRESS.")
    return wallet


def _signer_for(wallet: Wallet) -> XRPLWallet:
    """Decrypt a recipient's seed just long enough to sign (FR-WAL-04)."""
    return XRPLWallet.from_seed(crypto.decrypt_seed(wallet.encrypted_private_key))


def _classify(exc: Exception) -> tuple[str, str]:
    """Map a submission exception to (resolution, result_code).

    Only two messages prove a transaction did not move funds: a validated
    non-success, and a malformed (tem*) rejection. Everything else — a timeout,
    a failed RPC, an unrecognised error — is UNKNOWN, because the transaction is
    signed and may already be in a ledger this process never saw.
    """
    text = str(exc)
    if not isinstance(exc, XRPLReliableSubmissionException):
        # e.g. XRPLRequestFailureException: the RPC failed, and we cannot tell
        # whether the server took the transaction before it did.
        return UNKNOWN, "submission_error"

    validated = _VALIDATED_FAILURE.match(text)
    if validated:
        return FAILED, validated.group(1)

    malformed = _MALFORMED.match(text)
    if malformed:
        return FAILED, malformed.group(1)

    if "LastLedgerSequence" in text:
        # Gave up waiting. xrpl-py raises this without a final lookup, so the
        # transaction may have been validated in the last ledger before the
        # deadline. Unknowable here; an operator resolves it from the ledger.
        return UNKNOWN, "submission_timeout"

    return UNKNOWN, "submission_error"


async def submit(
    tx: XRPLTransaction,
    signer: XRPLWallet,
    client: AsyncJsonRpcClient,
    on_signed: Optional[OnSigned] = None,
    on_signed_tx: Optional[OnSignedTx] = None,
    submitted_ledger_index: Optional[int] = None,
) -> XRPLResult:
    """Sign, submit, and wait for a validated outcome (FR-WAL-06).

    The returned resolution says what may be done next: NOT_SUBMITTED is safe to
    retry, FAILED provably moved nothing, and UNKNOWN must be neither retried nor
    reversed until the ledger is consulted.

    on_signed gets just the hash; on_signed_tx additionally gets the ledger range
    needed to reconcile an unknown outcome. Both run after signing and before
    submission, and an exception from either propagates before anything is sent.
    """
    try:
        signed = await autofill_and_sign(tx, client, signer)
    except XRPLException as exc:
        logger.warning("XRPL sign failed for %s: %s", tx.account, type(exc).__name__)
        return XRPLResult(False, "sign_error", None, str(exc), outcome=NOT_SUBMITTED)

    tx_hash = signed.get_hash()
    # Persist before submitting. If this raises, nothing is sent — better a
    # transaction that never existed than one the database cannot account for.
    if on_signed is not None:
        await on_signed(tx_hash)
    if on_signed_tx is not None:
        await on_signed_tx(
            SignedTx(
                tx_hash=tx_hash,
                last_ledger_sequence=signed.last_ledger_sequence,
                submitted_ledger_index=submitted_ledger_index,
            )
        )
    try:
        resp = await submit_and_wait(signed, client)
    except XRPLException as exc:
        resolution, code = _classify(exc)
        logger.warning(
            "XRPL %s %s: %s (%s)", tx.transaction_type.value, tx_hash, code, resolution
        )
        return XRPLResult(False, code, tx_hash, str(exc), outcome=resolution)

    code = resp.result["meta"]["TransactionResult"]
    logger.info("XRPL %s %s: %s", tx.transaction_type.value, tx_hash, code)
    success = code == SUCCESS
    return XRPLResult(success, code, tx_hash, outcome=SUCCEEDED if success else FAILED)


async def get_uctusd_balance(
    address: str, client: Optional[AsyncJsonRpcClient] = None
) -> Optional[Decimal]:
    """On-ledger UCTUSD balance, or None if the account has no UCTUSD trust line."""
    client = client or get_client()
    resp = await client.request(AccountLines(account=address, peer=settings.xrpl_issuer_address))
    for line in resp.result.get("lines", []):
        if line["currency"] == settings.xrpl_currency_code:
            return Decimal(line["balance"])
    return None


async def get_transaction_result(
    tx_hash: str, client: Optional[AsyncJsonRpcClient] = None
) -> Optional[str]:
    """Final result code of a transaction, or None if it is not in a validated ledger."""
    client = client or get_client()
    resp = await client.request(Tx(transaction=tx_hash))
    if not resp.is_successful() or not resp.result.get("validated"):
        return None
    return resp.result["meta"]["TransactionResult"]


async def get_wallet_for_user(db: AsyncSession, user_id: uuid.UUID) -> Optional[Wallet]:
    result = await db.execute(select(Wallet).where(Wallet.user_id == user_id))
    return result.scalar_one_or_none()


async def ensure_trust_line(
    db: AsyncSession, wallet: Wallet, client: Optional[AsyncJsonRpcClient] = None
) -> XRPLResult:
    """TrustSet recipient -> issuer, once (FR-WAL-02). Safe to retry after a failure."""
    if wallet.trust_set_complete:
        return XRPLResult(True, SUCCESS, None, "already set")

    client = client or get_client()
    result = await submit(
        TrustSet(
            account=wallet.xrpl_address,
            limit_amount=IssuedCurrencyAmount(
                currency=settings.xrpl_currency_code,
                issuer=settings.xrpl_issuer_address,
                value=TRUST_LINE_LIMIT,
            ),
        ),
        _signer_for(wallet),
        client,
    )
    if result.success:
        wallet.trust_set_complete = True
        db.add(wallet)
        await db.commit()
    return result


async def provision_wallet(
    db: AsyncSession, user: User, client: Optional[AsyncJsonRpcClient] = None
) -> Wallet:
    """Give a recipient their dedicated XRPL account, trust-lined to the issuer (FR-WAL-01..03).

    Idempotent: an existing wallet is reused and only its trust line is retried.
    The row is committed before TrustSet, so a failed TrustSet leaves a wallet with
    trust_set_complete=False for the next attempt rather than an orphaned account.

    Accounts are funded by the Testnet faucet; a mainnet deployment would instead
    fund the reserve with an XRP payment from the treasury.
    """
    client = client or get_client()

    wallet = await get_wallet_for_user(db, user.id)
    if wallet is None:
        account = await generate_faucet_wallet(client)
        wallet = Wallet(
            id=uuid.uuid4(),
            user_id=user.id,
            xrpl_address=account.classic_address,
            encrypted_private_key=crypto.encrypt_seed(account.seed),
            key_encryption_key_id=crypto.key_id(),
        )
        db.add(wallet)
        # Holding a wallet makes them a recipient (shows the Wallet screen).
        user.can_receive = True
        db.add(user)
        await db.commit()
        await db.refresh(wallet)
        logger.info("Provisioned XRPL wallet %s for user %s", wallet.xrpl_address, user.id)

    result = await ensure_trust_line(db, wallet, client)
    if not result.success:
        logger.warning("TrustSet pending for %s: %s", wallet.xrpl_address, result.result_code)
    return wallet


async def send_from_treasury(
    destination: str,
    amount: Decimal,
    client: Optional[AsyncJsonRpcClient] = None,
    on_signed: Optional[OnSigned] = None,
    on_signed_tx: Optional[OnSignedTx] = None,
) -> XRPLResult:
    """Treasury -> recipient UCTUSD settlement payment (used by the settlement worker).

    on_signed_tx is the one to use: like burn_to_issuer, it hands back the ledger
    range the hash can appear in, which is what lets an admin retry prove a
    payment is dead instead of inferring it from elapsed time. The validated
    ledger index is read before signing, so the range cannot start after the
    payment was already included.
    """
    client = client or get_client()
    treasury = load_treasury()

    submitted_ledger_index: Optional[int] = None
    if on_signed_tx is not None:
        submitted_ledger_index = await get_latest_validated_ledger_sequence(client)

    return await submit(
        Payment(account=treasury.classic_address, destination=destination, amount=uctusd(amount)),
        treasury,
        client,
        on_signed,
        on_signed_tx=on_signed_tx,
        submitted_ledger_index=submitted_ledger_index,
    )


async def burn_to_issuer(
    wallet: Wallet,
    amount: Decimal,
    client: Optional[AsyncJsonRpcClient] = None,
    on_signed_tx: Optional[OnSignedTx] = None,
) -> XRPLResult:
    """Recipient -> issuer UCTUSD payment, which destroys the tokens (FR-CO-05).

    Same Payment proven in Phase 5, plus pre-submission persistence: the validated
    ledger index is read before signing so on_signed_tx receives the full range
    [submitted_ledger_index, last_ledger_sequence] in which the hash could land.
    A cash-out whose outcome is never observed is resolved from that range alone.
    """
    client = client or get_client()

    submitted_ledger_index: Optional[int] = None
    if on_signed_tx is not None:
        # Read before signing: a later reading could sit after the tx was already
        # included, which would narrow the search range and lose the transaction.
        submitted_ledger_index = await get_latest_validated_ledger_sequence(client)

    return await submit(
        Payment(
            account=wallet.xrpl_address,
            destination=settings.xrpl_issuer_address,
            amount=uctusd(amount),
        ),
        _signer_for(wallet),
        client,
        on_signed_tx=on_signed_tx,
        submitted_ledger_index=submitted_ledger_index,
    )


async def get_latest_validated_ledger(client: Optional[AsyncJsonRpcClient] = None) -> int:
    """Index of the most recently validated ledger."""
    return await get_latest_validated_ledger_sequence(client or get_client())


def _parse_complete_ledgers(complete_ledgers: str) -> list[tuple[int, int]]:
    """Parse a server's `complete_ledgers` string, e.g. "32570-97531234,97531240-97531250"."""
    ranges: list[tuple[int, int]] = []
    for chunk in (complete_ledgers or "").split(","):
        chunk = chunk.strip()
        if not chunk or chunk == "empty":
            continue
        try:
            if "-" in chunk:
                low, high = chunk.split("-", 1)
                ranges.append((int(low), int(high)))
            else:
                ranges.append((int(chunk), int(chunk)))
        except ValueError:
            continue  # unparseable chunk: treat as no coverage rather than guessing
    return ranges


async def has_complete_ledger_range(
    start: int, end: int, client: Optional[AsyncJsonRpcClient] = None
) -> bool:
    """True only if the server holds every ledger in [start, end] in one unbroken range.

    Reconcile needs this before it can call a missing transaction definitively
    failed: if the server has a gap in that window, the transaction could have
    been validated in a ledger the server simply cannot see.
    """
    client = client or get_client()
    resp = await client.request(ServerInfo())
    if not resp.is_successful():
        return False
    complete = resp.result.get("info", {}).get("complete_ledgers", "")
    return any(low <= start and end <= high for low, high in _parse_complete_ledgers(complete))
