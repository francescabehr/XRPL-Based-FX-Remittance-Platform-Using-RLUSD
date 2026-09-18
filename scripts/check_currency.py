"""Read the treasury wallet's trust lines to discover the real on-ledger UCTUSD currency code.

XRPL currency codes are either exactly 3 characters or a 160-bit (40-hex-char) value, so
"UCTUSD" cannot be the literal on-ledger code. Copy the printed `currency` value verbatim
into XRPL_CURRENCY_CODE — never guess it.

Usage: python scripts/check_currency.py
"""

from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountLines

JSON_RPC = "https://s.altnet.rippletest.net:51234"
TREASURY = "rMcBddj7AD6aEFoPSeSL8HpqVJMMxaezoz"
EXPECTED_ISSUER = "rELez4x4Zqv3KYqboYVfrYPF8521Ycbxa5"


def main() -> None:
    client = JsonRpcClient(JSON_RPC)
    resp = client.request(AccountLines(account=TREASURY))
    lines = resp.result.get("lines", [])

    if not lines:
        print(f"No trust lines found on {TREASURY}.")
        return

    print(f"Trust lines on {TREASURY}:\n")
    for line in lines:
        currency = line["currency"]
        issuer = line["account"]
        match = "  <-- expected issuer" if issuer == EXPECTED_ISSUER else ""
        print(f"  currency={currency!r} (len={len(currency)})")
        print(f"  issuer={issuer}{match}")
        print(f"  balance={line['balance']}\n")

    print("Copy the `currency` value above verbatim into XRPL_CURRENCY_CODE.")


if __name__ == "__main__":
    main()
