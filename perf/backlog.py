"""
Phase 9: pre-fill the settlement queue for the worker-scaling measurement.

Books N sends through the real cash-in code (quote, limit check, simulated card,
admin confirmation, queue publish), one per seeded sender, so every run of the
drain starts from the same backlog: N confirmed cash-ins, N settlement messages,
N recipients with no wallet yet. No worker should be running while this runs.

Run by perf/run_all.py, never against the real queue.

Usage: python perf/backlog.py 30
"""

import asyncio
import sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, engine
from app.models.beneficiary import Beneficiary
from app.models.user import User
from app.services import cashin_service
from app.services.cashin_service import AcceptedQuote, MockCard
from app.services.fx_service import quote_for

AMOUNT = Decimal("100")
CARD = MockCard(number="4242 4242 4242 4242", expiry="12/30", cvv="123", name="Load Test")


async def main(count: int) -> None:
    async with AsyncSessionLocal() as db:
        admin = (
            await db.execute(select(User).where(User.email == settings.admin_email.lower().strip()))
        ).scalar_one()
        for i in range(count):
            sender = (
                await db.execute(select(User).where(User.email == f"perf.sender{i}@loadtest.local"))
            ).scalar_one()
            beneficiary = (
                await db.execute(select(Beneficiary).where(Beneficiary.sender_id == sender.id))
            ).scalar_one()
            quote = await quote_for(db, AMOUNT, beneficiary.payout_currency)
            txn = await cashin_service.create_remittance(
                db, sender, beneficiary_id=beneficiary.id, zar_amount=AMOUNT, card=CARD,
                accepted=AcceptedQuote(quote.exchange_rate, quote.transaction_fee, quote.uctusd_amount),
            )
            await cashin_service.mark_cashin_received(db, txn, admin)
    await engine.dispose()
    print(f"Backlog: {count} confirmed cash-ins queued for settlement.")


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
