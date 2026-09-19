"""
FR-WAL-01..04  XRPL surface: recipient provisioning, TrustSet, UCTUSD payments, burn.

Every call here was first proven against Testnet by scripts/xrpl_smoke.py — keep
them in step. Rules this module enforces:

- The currency code, issuer and treasury all come from settings, never constants.
- Seeds are decrypted only here (via security/crypto.py), only at signing time,
  and never logged, returned, or stored in plaintext (FR-WAL-03/04).
- Transactions are signed before submission, so the hash is known even when the
  ledger rejects them. Failures come back as an XRPLResult carrying the result
  code (e.g. tecPATH_DRY), never as a raised exception (FR-WAL-07).

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
from xrpl.asyncio.transaction import autofill_and_sign, submit_and_wait
from xrpl.asyncio.wallet import generate_faucet_wallet
from xrpl.constants import XRPLException
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountLines, Tx
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

_RESULT_CODE = re.compile(r"\bte[a-z][A-Z_]+\b")

# Called with the tx hash after signing and before submission, so a caller can
# persist the hash first — if the process dies mid-submit, the payment can still
# be traced on the ledger instead of blindly re-sent.
OnSigned = Callable[[str], Awaitable[None]]


class XRPLConfigError(RuntimeError):
    """Treasury seed/address or other XRPL settings are missing or inconsistent."""


@dataclass(frozen=True)
class XRPLResult:
    """Outcome of one submitted transaction. tx_hash is set whenever signing succeeded."""

    success: bool
    result_code: str
    tx_hash: Optional[str]
    message: str = ""


def get_client() -> AsyncJsonRpcClient:
    return AsyncJsonRpcClient(settings.xrpl_json_rpc)


def uctusd(value: Decimal) -> IssuedCurrencyAmount:
    """A UCTUSD amount using the ledger-verified currency code (6 dp, positive)."""
    value = Decimal(value).quantize(QUANT_UCTUSD, rounding=ROUND_HALF_UP)
    if value <= 0:
        raise ValueError("UCTUSD amount must be positive.")
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


def _result_code(exc: Exception) -> str:
    match = _RESULT_CODE.search(str(exc))
    return match.group(0) if match else "submission_error"


async def submit(
    tx: XRPLTransaction,
    signer: XRPLWallet,
    client: AsyncJsonRpcClient,
    on_signed: Optional[OnSigned] = None,
) -> XRPLResult:
    """Sign, submit, and wait for a validated outcome (FR-WAL-06).

    A "sign_error" result means nothing reached the ledger, so it is safe to retry.
    Any other failure has a hash and may need a ledger check before retrying.
    """
    try:
        signed = await autofill_and_sign(tx, client, signer)
    except XRPLException as exc:
        logger.warning("XRPL sign failed for %s: %s", tx.account, type(exc).__name__)
        return XRPLResult(False, "sign_error", None, str(exc))

    tx_hash = signed.get_hash()
    if on_signed is not None:
        await on_signed(tx_hash)
    try:
        resp = await submit_and_wait(signed, client)
    except XRPLException as exc:
        code = _result_code(exc)
        logger.warning("XRPL %s %s failed: %s", tx.transaction_type.value, tx_hash, code)
        return XRPLResult(False, code, tx_hash, str(exc))

    code = resp.result["meta"]["TransactionResult"]
    logger.info("XRPL %s %s: %s", tx.transaction_type.value, tx_hash, code)
    return XRPLResult(code == SUCCESS, code, tx_hash)


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
) -> XRPLResult:
    """Treasury -> recipient UCTUSD settlement payment (used by the settlement worker)."""
    treasury = load_treasury()
    return await submit(
        Payment(account=treasury.classic_address, destination=destination, amount=uctusd(amount)),
        treasury,
        client or get_client(),
        on_signed,
    )


async def burn_to_issuer(
    wallet: Wallet, amount: Decimal, client: Optional[AsyncJsonRpcClient] = None
) -> XRPLResult:
    """Recipient -> issuer UCTUSD payment, which destroys the tokens (used by Phase 7 cash-out)."""
    return await submit(
        Payment(
            account=wallet.xrpl_address,
            destination=settings.xrpl_issuer_address,
            amount=uctusd(amount),
        ),
        _signer_for(wallet),
        client or get_client(),
    )
