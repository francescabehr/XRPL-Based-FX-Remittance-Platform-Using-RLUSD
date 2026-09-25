from datetime import date

import asyncio

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kyc import KYCSubmission
from app.models.user import KYCStatus, User
from app.services.auth_service import create_user
from tests.conftest import _TestSession
from app.services.kyc_service import (
    KYCReviewError,
    approve_kyc,
    get_active_kyc,
    reject_kyc,
    submit_kyc,
)


async def _make_user(db, suffix: str):
    return await create_user(
        db,
        full_name=f"User {suffix}",
        email=f"user{suffix}@example.com",
        mobile=f"+2780000{suffix}",
        password="TestPass1!",
    )


async def _make_admin(db, suffix: str):
    return await create_user(
        db,
        full_name=f"Admin {suffix}",
        email=f"admin{suffix}@example.com",
        mobile=f"+2790000{suffix}",
        password="AdminPass1!",
        is_admin=True,
    )


async def test_submit_kyc_sets_pending(db: AsyncSession):
    user = await _make_user(db, "k01")
    await submit_kyc(
        db, user,
        full_name="User K01",
        date_of_birth=date(1990, 1, 1),
        nationality="South African",
        id_number="9001010001087",
        residential_address="1 Main St, Cape Town",
        mobile="+27801234567",
        email="user.k01@example.com",
        source_of_funds="Salary",
    )
    assert user.kyc_status == KYCStatus.pending
    kyc = await get_active_kyc(db, user.id)
    assert kyc is not None
    assert kyc.status.value == "pending"


async def test_underage_kyc_rejected(db: AsyncSession):
    user = await _make_user(db, "k02")
    with pytest.raises(ValueError, match="18"):
        await submit_kyc(
            db, user,
            full_name="Young User",
            date_of_birth=date(2015, 6, 15),
            nationality="South African",
            id_number="1506150001087",
            residential_address="2 Young St",
            mobile="+27802222222",
            email="young@example.com",
            source_of_funds="Pocket money",
        )


async def test_approve_kyc(db: AsyncSession):
    user = await _make_user(db, "k03")
    admin = await _make_admin(db, "k03")
    sub = await submit_kyc(
        db, user,
        full_name="User K03",
        date_of_birth=date(1985, 3, 10),
        nationality="South African",
        id_number="8503100001087",
        residential_address="3 Test Rd",
        mobile="+27803333333",
        email="k03@example.com",
        source_of_funds="Business",
    )
    await approve_kyc(db, sub, admin)
    assert sub.status.value == "approved"
    assert sub.reviewed_by == admin.id
    assert user.kyc_status == KYCStatus.approved


async def test_reject_kyc(db: AsyncSession):
    user = await _make_user(db, "k04")
    admin = await _make_admin(db, "k04")
    sub = await submit_kyc(
        db, user,
        full_name="User K04",
        date_of_birth=date(1980, 7, 20),
        nationality="South African",
        id_number="8007200001087",
        residential_address="4 Test Ave",
        mobile="+27804444444",
        email="k04@example.com",
        source_of_funds="Savings",
    )
    await reject_kyc(db, sub, admin, reason="ID number does not match DOB.")
    assert sub.status.value == "rejected"
    assert sub.rejection_reason == "ID number does not match DOB."
    assert user.kyc_status == KYCStatus.rejected


# --- the review guard (AUDIT #9) ---
#
# approve_kyc / reject_kyc wrote submission.status and user.kyc_status
# unconditionally, so a stale link, a double-clicked button or a second admin
# could re-review a finished submission. Approving an already-rejected one
# handed the user the ability to send money. These prove the guard the way the
# conditional-UPDATE tests elsewhere do: by showing the second call is refused
# AND changes nothing.

async def _submitted(db, suffix):
    user = await _make_user(db, suffix)
    admin = await _make_admin(db, suffix)
    sub = await submit_kyc(
        db, user,
        full_name=f"User {suffix}",
        date_of_birth=date(1990, 1, 1),
        nationality="South African",
        id_number="9001010001087",
        residential_address="9 Guard St",
        mobile=f"+2781000{suffix}",
        email=f"guard{suffix}@example.com",
        source_of_funds="Salary",
    )
    return user, admin, sub


async def test_approve_after_reject_is_refused_and_changes_nothing(db: AsyncSession):
    """The dangerous direction: it would flip a rejected user to approved."""
    user, admin, sub = await _submitted(db, "g01")
    await reject_kyc(db, sub, admin, reason="ID number does not match DOB.")

    with pytest.raises(KYCReviewError, match="already been reviewed"):
        await approve_kyc(db, sub, admin)

    await db.refresh(sub)
    await db.refresh(user)
    assert sub.status.value == "rejected"
    assert sub.rejection_reason == "ID number does not match DOB."
    assert user.kyc_status == KYCStatus.rejected     # NOT flipped to approved


async def test_reject_after_approve_is_refused_and_changes_nothing(db: AsyncSession):
    user, admin, sub = await _submitted(db, "g02")
    await approve_kyc(db, sub, admin)

    with pytest.raises(KYCReviewError, match="already been reviewed"):
        await reject_kyc(db, sub, admin, reason="changed my mind")

    await db.refresh(sub)
    await db.refresh(user)
    assert sub.status.value == "approved"
    assert sub.rejection_reason is None
    assert user.kyc_status == KYCStatus.approved


async def test_approving_twice_is_refused(db: AsyncSession):
    """A double-clicked approve button."""
    user, admin, sub = await _submitted(db, "g03")
    await approve_kyc(db, sub, admin)
    first_reviewed_at = sub.reviewed_at

    with pytest.raises(KYCReviewError, match="already been reviewed"):
        await approve_kyc(db, sub, admin)

    await db.refresh(sub)
    assert sub.reviewed_at == first_reviewed_at   # the original review stands
    assert user.kyc_status == KYCStatus.approved


async def test_only_one_of_two_concurrent_reviews_wins(db: AsyncSession):
    """Two admins working the same queue: the conditional lets exactly one through."""
    user, admin, sub = await _submitted(db, "g04")

    async def review(fn, **kwargs):
        async with _TestSession() as s:
            mine = await s.get(KYCSubmission, sub.id)
            try:
                await fn(s, mine, admin, **kwargs)
                return "applied"
            except KYCReviewError:
                return "refused"

    results = await asyncio.gather(
        review(approve_kyc),
        review(reject_kyc, reason="Documents unreadable."),
    )

    assert sorted(results) == ["applied", "refused"]
    async with _TestSession() as s:
        final_user = await s.get(User, user.id)
        final_sub = await s.get(KYCSubmission, sub.id)
    # Whichever won, the submission and the user agree — never one of each.
    assert (final_sub.status.value, final_user.kyc_status.value) in {
        ("approved", "approved"),
        ("rejected", "rejected"),
    }


async def test_reject_requires_reason(db: AsyncSession):
    user = await _make_user(db, "k05")
    admin = await _make_admin(db, "k05")
    sub = await submit_kyc(
        db, user,
        full_name="User K05",
        date_of_birth=date(1975, 11, 5),
        nationality="South African",
        id_number="7511050001087",
        residential_address="5 Blank St",
        mobile="+27805555555",
        email="k05@example.com",
        source_of_funds="Investment",
    )
    with pytest.raises(ValueError, match="reason"):
        await reject_kyc(db, sub, admin, reason="   ")
