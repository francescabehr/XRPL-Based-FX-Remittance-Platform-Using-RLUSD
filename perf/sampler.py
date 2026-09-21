"""
Phase 9 step 3: sample the queue and the settlement pipeline once a second.

Locust measures HTTP only. The brief also asks for message-queue throughput and
UCTUSD transaction processing time, which live behind the queue, so this records
queue depth and transaction states to a CSV alongside the Locust output.

Usage (run it for the duration of a load test, then stop with Ctrl-C):
    python perf/sampler.py perf/results/queue.csv
"""

import asyncio
import csv
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis import Redis
from rq import Queue
from rq.registry import FinishedJobRegistry, StartedJobRegistry
from sqlalchemy import func, select

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models.transaction import SettlementStatus, Transaction

COLUMNS = ["t", "queue_depth", "jobs_running", "jobs_finished", "queued", "processing", "completed", "failed"]


async def sample_db(db) -> dict:
    rows = (
        await db.execute(
            select(Transaction.settlement_status, func.count())
            .group_by(Transaction.settlement_status)
        )
    ).all()
    counts = {status.value: count for status, count in rows}
    return {s.value: counts.get(s.value, 0) for s in SettlementStatus}


async def main(path: Path) -> None:
    connection = Redis.from_url(settings.redis_url)
    queue = Queue("settlement", connection=connection)
    started = StartedJobRegistry(queue=queue)
    finished = FinishedJobRegistry(queue=queue)

    path.parent.mkdir(parents=True, exist_ok=True)
    start = time.time()
    print(f"Sampling every second -> {path}  (Ctrl-C to stop)")

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        try:
            while True:
                async with AsyncSessionLocal() as db:
                    states = await sample_db(db)
                writer.writerow({
                    "t": round(time.time() - start, 1),
                    "queue_depth": len(queue),
                    "jobs_running": len(started),
                    "jobs_finished": len(finished),
                    **{k: states[k] for k in ("queued", "processing", "completed", "failed")},
                })
                handle.flush()
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await engine.dispose()
            print(f"\nStopped. {path} written.")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1] if len(sys.argv) > 1 else "perf/results/queue.csv")))
