"""KYC source of funds: a dropdown, with an "Other" text box whose text is stored."""
import re

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kyc import KYCSubmission
from app.routers.kyc import SOURCES_OF_FUNDS
from tests.remit_helpers import logged_in, registered_recipient


def kyc_form(user, **overrides):
    data = {
        "full_name": user.full_name, "date_of_birth": "1990-05-17", "nationality": "South African",
        "id_number": "9005170000000", "residential_address": "12 Long Street, Cape Town",
        "mobile": user.mobile, "email": user.email, "source_of_funds": "Salary or wages",
    }
    data.update(overrides)
    return data


async def stored_source(db: AsyncSession, user) -> list[str]:
    rows = await db.execute(select(KYCSubmission.source_of_funds).where(KYCSubmission.user_id == user.id))
    return list(rows.scalars())


async def test_form_offers_a_dropdown_with_other(client: AsyncClient, db: AsyncSession):
    with logged_in(await registered_recipient(db)):
        page = (await client.get("/kyc")).text
    field = re.search(r'<select name="source_of_funds"[^>]*id="kyc-source_of_funds"[^>]*>(.*?)</select>', page, re.S)
    assert field, "source_of_funds is a <select> with its original name and id"
    for option in SOURCES_OF_FUNDS + ["Other"]:
        assert f'<option value="{option}"' in field.group(1)
    assert 'name="source_of_funds_other"' in page and 'id="kyc-source_of_funds_other"' in page
    assert 'for="kyc-source_of_funds_other"' in page


async def test_a_listed_option_is_stored_as_is(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    with logged_in(user):
        response = await client.post("/kyc", data=kyc_form(user, source_of_funds="Pension"), follow_redirects=False)
    assert response.status_code == 302
    assert await stored_source(db, user) == ["Pension"]


async def test_other_stores_the_description(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    with logged_in(user):
        response = await client.post("/kyc", data=kyc_form(
            user, source_of_funds="Other", source_of_funds_other="  Freelance design work  "), follow_redirects=False)
    assert response.status_code == 302
    assert await stored_source(db, user) == ["Freelance design work"]


async def test_other_without_a_description_is_rejected(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    with logged_in(user):
        response = await client.post("/kyc", data=kyc_form(user, source_of_funds="Other", source_of_funds_other=" "))
    assert response.status_code == 400
    assert "Please describe your source of funds." in response.text
    assert re.search(r'<option value="Other"\s+selected', response.text)  # the choice is kept
    assert await stored_source(db, user) == []


async def test_no_choice_is_rejected(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    with logged_in(user):
        response = await client.post("/kyc", data=kyc_form(user, source_of_funds=""))
    assert response.status_code == 400 and "Please choose your source of funds." in response.text
    assert await stored_source(db, user) == []


async def test_error_rerender_keeps_the_other_text(client: AsyncClient, db: AsyncSession):
    user = await registered_recipient(db)
    with logged_in(user):  # a too-short ID fails in the service; the Other text must survive
        response = await client.post("/kyc", data=kyc_form(
            user, id_number="123", source_of_funds="Other", source_of_funds_other="Crypto mining"))
    assert response.status_code == 400
    assert re.search(r'<option value="Other"\s+selected', response.text)
    assert 'value="Crypto mining"' in response.text
