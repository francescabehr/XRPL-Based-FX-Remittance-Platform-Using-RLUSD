"""
Phase 9: the settlement worker with a simulated ledger.

Everything real is real — Redis, RQ, the claim, the database writes, the wallet
credit — except the two XRPL calls, which are replaced with a fixed delay. This
measures what the platform controls (queue throughput, DB contention, worker
drain rate) at a load the real Testnet could not serve: hundreds of faucet
accounts and treasury payments are rate-limited and spend real test funds.

Real ledger timings are measured separately by perf/measure_xrpl.py, and the
report keeps the two apart.

Usage:
    python perf/perf_worker.py                 # 4.0 s simulated ledger round-trip
    PERF_XRPL_LATENCY=6 python perf/perf_worker.py
"""

import asyncio
import os
import sys
import uuid
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis import Redis
from rq import Queue, SimpleWorker
from sqlalchemy import select

from app.models.wallet import Wallet
from app.security import crypto
from app.services import queue_service, xrpl_service
from app.services.xrpl_service import XRPLResult

LATENCY = float(os.environ.get("PERF_XRPL_LATENCY", "4.0"))
# A throwaway seed for an account that does not exist on any ledger. It signs
# nothing here, but keeps the encryption path (and the wallet row) identical to
# production. Never reuse a real wallet's seed for this.
DUMMY_SEED = "sEdVqyPCYmX874wCnqCuZg6GtsMJrU1"


async def fake_provision_wallet(db, user, client=None):
    """Same row the real provisioner writes, without the faucet or TrustSet."""
    wallet = (await db.execute(select(Wallet).where(Wallet.user_id == user.id))).scalar_one_or_none()
    if wallet is None:
        wallet = Wallet(
            id=uuid.uuid4(), user_id=user.id,
            xrpl_address=f"rPERF{uuid.uuid4().hex[:20]}",
            encrypted_private_key=crypto.encrypt_seed(DUMMY_SEED),
            key_encryption_key_id=crypto.key_id(),
        )
        user.can_receive = True
        db.add(wallet)
    wallet.trust_set_complete = True
    await db.commit()
    await asyncio.sleep(LATENCY / 2)  # stands in for faucet + TrustSet
    return wallet


async def fake_send_from_treasury(destination, amount: Decimal, client=None, on_signed=None, **kwargs):
    tx_hash = f"PERF{uuid.uuid4().hex.upper()[:60]}"
    if on_signed:
        await on_signed(tx_hash)
    on_signed_tx = kwargs.get("on_signed_tx")
    if on_signed_tx:  # Phase 7 added a richer callback; support both
        await on_signed_tx(xrpl_service.SignedTx(tx_hash=tx_hash, last_ledger_sequence=None,
                                                 submitted_ledger_index=None))
    await asyncio.sleep(LATENCY)  # stands in for submit + validation
    return XRPLResult(True, xrpl_service.SUCCESS, tx_hash)


def main() -> None:
    xrpl_service.provision_wallet = fake_provision_wallet
    xrpl_service.send_from_treasury = fake_send_from_treasury

    print(f"perf worker: simulated ledger, {LATENCY}s payment latency. Real XRPL calls are NOT made.")
    connection = Redis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
    queue = Queue(queue_service.QUEUE_NAME, connection=connection)
    SimpleWorker([queue], connection=connection).work(with_scheduler=True)


if __name__ == "__main__":
    main()
