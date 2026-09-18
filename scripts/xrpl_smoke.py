"""Phase 5 de-risk: prove the whole UCTUSD on-chain path against XRPL Testnet.

Throwaway script — not imported by the app. Walks BUILD_PLAN.md Phase 5 steps 1-9:
treasury load, recipient provisioning, reserve check, TrustSet, treasury -> recipient
payment, on-ledger validation, burn round-trip, Fernet round-trip, and the failure
result codes the settlement worker will branch on.

Reads every value from settings (.env). Seeds are never printed.

Usage: python scripts/xrpl_smoke.py
"""

import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
from xrpl.clients import JsonRpcClient
from xrpl.models.amounts import IssuedCurrencyAmount
from xrpl.models.requests import AccountInfo, AccountLines, ServerInfo
from xrpl.models.transactions import Payment, TrustSet
from xrpl.transaction import submit_and_wait
from xrpl.utils import drops_to_xrp
from xrpl.wallet import Wallet, generate_faucet_wallet

from app.config import settings

CURRENCY = settings.xrpl_currency_code
ISSUER = settings.xrpl_issuer_address
EXPLORER = "https://testnet.xrpl.org/transactions/"

failure_codes: dict[str, str] = {}


def step(n, title):
    print(f"\n[{n}] {title}")


def submit(client, tx, wallet):
    """Submit, wait for validation, and return (result_code, hash)."""
    resp = submit_and_wait(tx, client, wallet)
    return resp.result["meta"]["TransactionResult"], resp.result["hash"]


def expect_success(client, tx, wallet, label):
    result, tx_hash = submit(client, tx, wallet)
    assert result == "tesSUCCESS", f"{label} failed: {result}"
    print(f"    {label}: {result}  {EXPLORER}{tx_hash}")
    return tx_hash


def uctusd_balance(client, address):
    lines = client.request(AccountLines(account=address, peer=ISSUER)).result.get("lines", [])
    for line in lines:
        if line["currency"] == CURRENCY:
            return Decimal(line["balance"])
    return None


def uctusd(value):
    return IssuedCurrencyAmount(currency=CURRENCY, issuer=ISSUER, value=value)


def record_failure(label, fn):
    """Run a transaction expected to fail and record how it fails."""
    try:
        outcome = fn()
    except Exception as exc:  # noqa: BLE001 — we want the exact failure surface
        outcome = f"{type(exc).__name__}: {exc}"
    failure_codes[label] = outcome
    print(f"    {label}: {outcome}")


def main() -> None:
    client = JsonRpcClient(settings.xrpl_json_rpc)

    step(1, "Client + treasury")
    treasury = Wallet.from_seed(settings.xrpl_platform_wallet_seed)
    assert treasury.classic_address == settings.xrpl_platform_wallet_address, "seed/address mismatch"
    treasury_before = uctusd_balance(client, treasury.classic_address)
    print(f"    treasury {treasury.classic_address} holds {treasury_before} UCTUSD")

    step(2, "Create recipient account (faucet)  FR-WAL-01")
    recip = generate_faucet_wallet(client)
    print(f"    recipient {recip.classic_address}")

    step(3, "Reserve check")
    ledger = client.request(ServerInfo()).result["info"]["validated_ledger"]
    base, inc = Decimal(str(ledger["reserve_base_xrp"])), Decimal(str(ledger["reserve_inc_xrp"]))
    xrp = drops_to_xrp(
        client.request(AccountInfo(account=recip.classic_address)).result["account_data"]["Balance"]
    )
    print(f"    balance {xrp} XRP; reserve base {base} + {inc} per trust line")
    assert xrp >= base + inc, "not enough XRP to hold a trust line"

    step(4, "TrustSet recipient -> issuer  FR-WAL-02")
    expect_success(
        client,
        TrustSet(account=recip.classic_address, limit_amount=uctusd("1000000")),
        recip,
        "TrustSet",
    )

    step(5, "Fund 10 UCTUSD from treasury")
    expect_success(
        client,
        Payment(account=treasury.classic_address, destination=recip.classic_address, amount=uctusd("10")),
        treasury,
        "Payment",
    )

    step(6, "Validate on-ledger")
    bal = uctusd_balance(client, recip.classic_address)
    assert bal == Decimal("10"), f"expected 10, got {bal}"
    print(f"    recipient holds {bal} UCTUSD (counterparty {ISSUER}) — rippling via issuer works")

    step(7, "Burn round-trip: 5 UCTUSD back to issuer")
    expect_success(
        client,
        Payment(account=recip.classic_address, destination=ISSUER, amount=uctusd("5")),
        recip,
        "Burn",
    )
    bal = uctusd_balance(client, recip.classic_address)
    assert bal == Decimal("5"), f"expected 5, got {bal}"
    print(f"    recipient now holds {bal} UCTUSD")

    step(8, "Fernet round-trip  FR-WAL-03")
    fernet = Fernet(settings.xrpl_encryption_key.encode())
    token = fernet.encrypt(recip.seed.encode())
    assert fernet.decrypt(token).decode() == recip.seed
    assert recip.seed.encode() not in token
    print(f"    encrypt/decrypt ok ({len(token)}-byte token, plaintext absent)")

    step(9, "Failure taxonomy")
    no_line = generate_faucet_wallet(client)
    record_failure(
        "payment to account with no trust line",
        lambda: submit(
            client,
            Payment(account=treasury.classic_address, destination=no_line.classic_address, amount=uctusd("1")),
            treasury,
        )[0],
    )
    record_failure(
        "payment exceeding balance (burn 1000, holds 5)",
        lambda: submit(
            client,
            Payment(account=recip.classic_address, destination=ISSUER, amount=uctusd("1000")),
            recip,
        )[0],
    )
    record_failure(
        "malformed currency code",
        lambda: submit(
            client,
            Payment(
                account=recip.classic_address,
                destination=ISSUER,
                amount=IssuedCurrencyAmount(currency="UCTUSD", issuer=ISSUER, value="1"),
            ),
            recip,
        )[0],
    )

    treasury_after = uctusd_balance(client, treasury.classic_address)
    print(f"\nTreasury: {treasury_before} -> {treasury_after} UCTUSD")
    print("All success steps passed. Failure codes:")
    for label, code in failure_codes.items():
        print(f"  - {label}: {code}")


if __name__ == "__main__":
    main()
