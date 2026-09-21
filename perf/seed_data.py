"""
Phase 9 step 1: synthetic users for the load test (Brief §7.iv).

Creates N sender/recipient pairs with Faker: KYC-approved senders, registered
recipients, and a beneficiary linking each pair, so load tests exercise the real
send path instead of a single hot row.

Every seeded account uses the email prefix `perf.` and one shared password, so
the data is easy to spot and remove. Passwords are hashed once and reused —
bcrypt is deliberately slow, and hashing 400 accounts individually would take
minutes without making the load test any more realistic.

Usage:
    python perf/seed_data.py            # 200 pairs (default)
    python perf/seed_data.py 50         # 50 pairs
    python perf/seed_data.py --purge    # delete seeded accounts and their data
"""

import asyncio
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from faker import Faker
from sqlalchemy import delete, func, select

from app.database import AsyncSessionLocal, engine
from app.models.beneficiary import Beneficiary, PayoutCurrency
from app.models.cashout import CashOutRequest
from app.models.kyc import KYCSubmission, KYCSubmissionStatus
from app.models.transaction import Transaction
from app.models.user import KYCStatus, User
from app.models.wallet import Wallet
from app.security.hashing import get_password_hash

PREFIX = "perf."
PASSWORD = "PerfTest123!"
DEFAULT_PAIRS = 200

# No en_ZA locale in Faker; zu_ZA + en_GB gives a plausible South African name mix.
fake = Faker(["zu_ZA", "en_GB"])


def _mobile(n: int) -> str:
    return f"+2765{n:07d}"


async def purge() -> None:
    async with AsyncSessionLocal() as db:
        ids = (
            await db.execute(select(User.id).where(User.email.like(f"{PREFIX}%")))
        ).scalars().all()
        if not ids:
            print("No seeded accounts found.")
            return
        # Children first, deepest reference last: cash-out requests point at wallets,
        # wallets and transactions point at users.
        await db.execute(delete(CashOutRequest).where(CashOutRequest.recipient_user_id.in_(ids)))
        await db.execute(
            delete(Transaction).where(
                Transaction.sender_id.in_(ids) | Transaction.recipient_user_id.in_(ids)
            )
        )
        await db.execute(delete(Wallet).where(Wallet.user_id.in_(ids)))
        await db.execute(delete(Beneficiary).where(Beneficiary.sender_id.in_(ids)))
        await db.execute(delete(KYCSubmission).where(KYCSubmission.user_id.in_(ids)))
        await db.execute(delete(User).where(User.id.in_(ids)))
        await db.commit()
        print(f"Purged {len(ids)} seeded accounts (and their beneficiaries/transactions).")


async def seed(pairs: int) -> None:
    password_hash = get_password_hash(PASSWORD)
    now = datetime.now(timezone.utc)

    async with AsyncSessionLocal() as db:
        existing = (
            await db.execute(select(func.count()).select_from(User).where(User.email.like(f"{PREFIX}%")))
        ).scalar_one()
        if existing:
            print(f"{existing} seeded accounts already exist — purge first to reseed.")
            return

        senders, recipients, beneficiaries, submissions = [], [], [], []
        for i in range(pairs):
            sender = User(
                id=uuid.uuid4(), email=f"{PREFIX}sender{i}@loadtest.local", mobile=_mobile(i * 2),
                full_name=fake.name(), password_hash=password_hash,
                can_send=True, kyc_status=KYCStatus.approved,
            )
            recipient = User(
                id=uuid.uuid4(), email=f"{PREFIX}recipient{i}@loadtest.local", mobile=_mobile(i * 2 + 1),
                full_name=fake.name(), password_hash=password_hash, can_send=False,
            )
            senders.append(sender)
            recipients.append(recipient)
            # An approved KYC record per sender, so the data matches a real approved user.
            submissions.append(KYCSubmission(
                id=uuid.uuid4(), user_id=sender.id, full_name=sender.full_name,
                date_of_birth=date(1990, 1, 1), nationality="South African",
                id_number=fake.numerify("#############"), residential_address=fake.address().replace("\n", ", "),
                mobile=sender.mobile, email=sender.email, source_of_funds="Salary",
                status=KYCSubmissionStatus.approved, submitted_at=now, reviewed_at=now,
            ))
            beneficiaries.append(Beneficiary(
                id=uuid.uuid4(), sender_id=sender.id, recipient_user_id=recipient.id,
                full_name=recipient.full_name, email=recipient.email, mobile=recipient.mobile,
                country=fake.random_element(["Zimbabwe", "Kenya", "Nigeria", "Ghana", "Malawi"]),
                payout_currency=fake.random_element([PayoutCurrency.USD, PayoutCurrency.ZAR]),
                relation_type=fake.random_element(["Parent", "Sibling", "Friend", "Spouse / Partner"]),
                is_active=True,
            ))

        db.add_all(senders + recipients + submissions + beneficiaries)
        await db.commit()

    print(f"Seeded {pairs} sender/recipient pairs ({pairs * 2} accounts), password {PASSWORD!r}.")
    print(f"Senders: {PREFIX}sender0..{pairs - 1}@loadtest.local (KYC approved, one beneficiary each)")


async def main() -> None:
    try:
        if "--purge" in sys.argv:
            await purge()
        else:
            count = next((int(a) for a in sys.argv[1:] if a.isdigit()), DEFAULT_PAIRS)
            await seed(count)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
