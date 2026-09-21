"""
Phase 9: real XRPL Testnet timings (Brief §7.iv, "UCTUSD transaction processing time").

The load test runs against a simulated ledger, so this measures the real thing on
its own: how long provisioning and a treasury payment actually take, end to end,
including waiting for a validated ledger.

It spends real Testnet UCTUSD (0.5 per payment) and creates one faucet account.

Usage:
    python perf/measure_xrpl.py          # 1 provision + 3 payments
    python perf/measure_xrpl.py 5        # 5 payments
"""

import asyncio
import json
import statistics
import sys
import time
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.models.wallet import Wallet
from app.security import crypto
from app.services import xrpl_service

AMOUNT = Decimal("0.5")
OUT = Path("perf/results/xrpl_timings.json")


class _NoDB:
    """ensure_trust_line only needs add/commit; nothing is persisted here."""

    def add(self, _):
        pass

    async def commit(self):
        pass


async def main(payments: int) -> None:
    client = xrpl_service.get_client()
    timings: dict[str, list[float]] = {"faucet_account": [], "trust_line": [], "payment": []}

    start = time.perf_counter()
    account = await xrpl_service.generate_faucet_wallet(client)
    timings["faucet_account"].append(time.perf_counter() - start)
    print(f"faucet account {account.classic_address}: {timings['faucet_account'][0]:.1f}s")

    wallet = Wallet(
        id=uuid.uuid4(), user_id=uuid.uuid4(), xrpl_address=account.classic_address,
        encrypted_private_key=crypto.encrypt_seed(account.seed),
        key_encryption_key_id=crypto.key_id(), trust_set_complete=False,
    )

    start = time.perf_counter()
    result = await xrpl_service.ensure_trust_line(_NoDB(), wallet, client)
    timings["trust_line"].append(time.perf_counter() - start)
    print(f"trust line: {timings['trust_line'][0]:.1f}s ({result.result_code})")

    for i in range(payments):
        start = time.perf_counter()
        result = await xrpl_service.send_from_treasury(wallet.xrpl_address, AMOUNT, client)
        elapsed = time.perf_counter() - start
        timings["payment"].append(elapsed)
        print(f"payment {i + 1}: {elapsed:.1f}s ({result.result_code}) {result.tx_hash}")
        if not result.success:
            break

    summary = {
        step: {
            "samples": len(values),
            "min_s": round(min(values), 2),
            "mean_s": round(statistics.fmean(values), 2),
            "max_s": round(max(values), 2),
        }
        for step, values in timings.items() if values
    }
    summary["first_transfer_total_s"] = round(
        timings["faucet_account"][0] + timings["trust_line"][0] + timings["payment"][0], 2
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(summary, indent=2))
    print(f"\n{json.dumps(summary, indent=2)}\nWritten to {OUT}")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
