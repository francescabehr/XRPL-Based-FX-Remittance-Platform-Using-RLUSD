"""Read the treasury wallet's trust lines to discover the real on-ledger UCTUSD currency code.

XRPL currency codes are either exactly 3 characters or a 160-bit (40-hex-char) value, so
"UCTUSD" cannot be the literal on-ledger code. Copy the printed `currency` value verbatim
into XRPL_CURRENCY_CODE — never guess it.

The endpoint, treasury and issuer come from .env like everywhere else, so switching
issuer stays a config change (CLAUDE.md §4). XRPL_CURRENCY_CODE is the one value this
script exists to discover, so it is the only one it does not read.

Usage: python scripts/check_currency.py
"""

import sys

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountLines

from app.config import settings


def main() -> None:
    treasury = settings.xrpl_platform_wallet_address
    expected_issuer = settings.xrpl_issuer_address
    if not treasury:
        sys.exit("XRPL_PLATFORM_WALLET_ADDRESS is not set in .env.")

    client = JsonRpcClient(settings.xrpl_json_rpc)
    resp = client.request(AccountLines(account=treasury))
    lines = resp.result.get("lines", [])

    if not lines:
        print(f"No trust lines found on {treasury}.")
        return

    print(f"Trust lines on {treasury}:\n")
    for line in lines:
        currency = line["currency"]
        issuer = line["account"]
        match = "  <-- expected issuer" if issuer == expected_issuer else ""
        print(f"  currency={currency!r} (len={len(currency)})")
        print(f"  issuer={issuer}{match}")
        print(f"  balance={line['balance']}\n")

    print("Copy the `currency` value above verbatim into XRPL_CURRENCY_CODE.")


if __name__ == "__main__":
    main()
