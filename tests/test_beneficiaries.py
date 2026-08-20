import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.auth_service import create_user
from app.services.beneficiary_service import (
    create_beneficiary,
    delete_beneficiary,
    get_beneficiary,
    list_beneficiaries,
    update_beneficiary,
)


async def _sender(db, suffix):
    return await create_user(
        db, full_name=f"Sender {suffix}", email=f"sender{suffix}@test.com",
        mobile=f"+2781100{suffix}", password="Pass1234!",
    )


async def _recipient(db, suffix):
    return await create_user(
        db, full_name=f"Recip {suffix}", email=f"recip{suffix}@test.com",
        mobile=f"+2781200{suffix}", password="Pass1234!",
    )


async def test_create_beneficiary(db: AsyncSession):
    sender = await _sender(db, "b01")
    ben = await create_beneficiary(
        db, sender,
        full_name="Jane Doe", email="jane@example.com", mobile=None,
        country="Zimbabwe", payout_currency="USD", relationship="Sibling",
    )
    assert ben.id is not None
    assert ben.sender_id == sender.id
    assert ben.payout_currency.value == "USD"
    assert ben.is_active is True


async def test_list_only_own_beneficiaries(db: AsyncSession):
    s1 = await _sender(db, "b02a")
    s2 = await _sender(db, "b02b")
    await create_beneficiary(db, s1, full_name="A", email="a@x.com", mobile=None,
                             country="Kenya", payout_currency="ZAR", relationship="Friend")
    await create_beneficiary(db, s2, full_name="B", email="b@x.com", mobile=None,
                             country="Kenya", payout_currency="ZAR", relationship="Friend")

    s1_bens = await list_beneficiaries(db, s1.id)
    assert len(s1_bens) == 1
    assert s1_bens[0].full_name == "A"


async def test_no_contact_raises(db: AsyncSession):
    sender = await _sender(db, "b03")
    with pytest.raises(ValueError, match="email or mobile"):
        await create_beneficiary(
            db, sender, full_name="No Contact", email=None, mobile=None,
            country="Nigeria", payout_currency="USD", relationship="Other",
        )


async def test_auto_links_existing_recipient(db: AsyncSession):
    sender = await _sender(db, "b04")
    recip = await _recipient(db, "b04")

    ben = await create_beneficiary(
        db, sender,
        full_name="Linked", email=recip.email, mobile=None,
        country="Tanzania", payout_currency="USD", relationship="Parent",
    )
    assert ben.recipient_user_id == recip.id


async def test_no_self_link(db: AsyncSession):
    sender = await _sender(db, "b05")
    ben = await create_beneficiary(
        db, sender,
        full_name="Self", email=sender.email, mobile=None,
        country="South Africa", payout_currency="ZAR", relationship="Other",
    )
    assert ben.recipient_user_id is None


async def test_edit_beneficiary(db: AsyncSession):
    sender = await _sender(db, "b06")
    ben = await create_beneficiary(
        db, sender, full_name="Old Name", email="old@x.com", mobile=None,
        country="Ghana", payout_currency="USD", relationship="Friend",
    )
    await update_beneficiary(
        db, ben, full_name="New Name", email="new@x.com", mobile=None,
        country="Ghana", payout_currency="ZAR", relationship="Sibling",
    )
    assert ben.full_name == "New Name"
    assert ben.payout_currency.value == "ZAR"


async def test_delete_beneficiary(db: AsyncSession):
    sender = await _sender(db, "b07")
    ben = await create_beneficiary(
        db, sender, full_name="To Delete", email="del@x.com", mobile=None,
        country="Uganda", payout_currency="USD", relationship="Other",
    )
    await delete_beneficiary(db, ben)
    assert ben.is_active is False

    bens = await list_beneficiaries(db, sender.id)
    assert all(b.id != ben.id for b in bens)


async def test_get_beneficiary_wrong_owner_returns_none(db: AsyncSession):
    s1 = await _sender(db, "b08a")
    s2 = await _sender(db, "b08b")
    ben = await create_beneficiary(
        db, s1, full_name="Private", email="priv@x.com", mobile=None,
        country="Rwanda", payout_currency="USD", relationship="Child",
    )
    result = await get_beneficiary(db, ben.id, s2.id)
    assert result is None
