"""Phase 6b: /profile is read-only; /profile/edit (email, mobile, name before KYC) and
/profile/password are separate pages."""
import re
import uuid
from datetime import date

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kyc import KYCSubmission, KYCSubmissionStatus
from app.models.user import KYCStatus, User
from app.security.hashing import verify_password
from app.services.auth_service import EMAIL_INVALID, MOBILE_INVALID, NAME_LOCKED
from app.services.kyc_service import submit_kyc
from tests.remit_helpers import logged_in, registered_recipient
from tests.test_shell import an_admin

PASSWORD = "Pass1234!"


def unique_email(stem: str) -> str:
    """Rows are committed and outlive a test, so every new address must be fresh."""
    return f"{stem}.{uuid.uuid4().hex[:8]}@example.com"


def unique_mobile() -> str:
    return f"+2781{int(uuid.uuid4().hex[:7], 16) % 10**7:07d}"


def details(user, **overrides):
    data = {"email": user.email, "mobile": user.mobile}
    data.update(overrides)
    return data


async def a_submission(db: AsyncSession, user, status=KYCSubmissionStatus.pending):
    sub = await submit_kyc(
        db, user, full_name=user.full_name, date_of_birth=date(1990, 1, 1), nationality="South African",
        id_number="9001015009087", residential_address="1 Main Rd, Cape Town", mobile=user.mobile,
        email=user.email, source_of_funds="Salary",
    )
    if status != KYCSubmissionStatus.pending:
        sub.status = status
        user.kyc_status = KYCStatus(status.value)
        await db.commit()
        await db.refresh(user)
        await db.refresh(sub)
    return sub


async def reloaded(db: AsyncSession, user) -> User:
    await db.refresh(user)
    return user


# ── Signed out ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/profile", "/profile/password"])
async def test_edits_require_login(client: AsyncClient, path):
    response = await client.post(path, data={"email": "a@example.com", "mobile": "+27820000000",
                                             "current_password": "x", "new_password": "y", "confirm_password": "y"},
                                 follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/login"


@pytest.mark.parametrize("path", ["/profile/edit", "/profile/password"])
async def test_edit_pages_require_login(client: AsyncClient, path):
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/login"


# ── Pages ─────────────────────────────────────────────────────────────────────

async def test_profile_is_read_only_with_buttons(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        page = (await client.get("/profile")).text
    assert '<form method="post"' not in page                   # the only form is base.html's confirm dialog
    assert 'href="/profile/edit"' in page and ">Edit details</a>" in page
    assert 'href="/profile/password"' in page and ">Change password</a>" in page


@pytest.mark.parametrize("path, action", [("/profile/edit", "/profile"), ("/profile/password", "/profile/password")])
async def test_edit_pages_post_back_and_cancel_to_profile(client: AsyncClient, db: AsyncSession, seed_tiers, path, action):
    user = await registered_recipient(db)
    with logged_in(user):
        page = (await client.get(path)).text
    assert f'<form method="post" action="{action}"' in page
    assert '<a href="/profile" class="btn btn-outline-secondary">Cancel</a>' in page
    assert page.count("<h1") == 1


async def test_the_details_form_is_filled_with_current_values(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        page = (await client.get("/profile/edit")).text
    assert f'value="{user.email}"' in page and f'value="{user.mobile}"' in page


# ── Email and mobile ──────────────────────────────────────────────────────────

async def test_email_and_mobile_update(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    email, mobile = unique_email("New.Address"), unique_mobile()
    with logged_in(user):
        response = await client.post("/profile", data=details(user, email=f"  {email.upper()} ", mobile=mobile),
                                     follow_redirects=False)
        assert response.status_code == 302 and response.headers["location"] == "/profile"
        page = (await client.get("/profile")).text
    user = await reloaded(db, user)
    assert user.email == email.lower() and user.mobile == mobile
    assert "Your details have been updated." in page


async def test_new_email_keeps_you_signed_in_and_is_the_next_login(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    old_email, new_email = user.email, unique_email("moved")
    assert (await client.post("/login", data={"email": old_email, "password": PASSWORD})).status_code in (200, 302)
    response = await client.post("/profile", data=details(user, email=new_email), follow_redirects=False)
    assert response.status_code == 302
    assert (await client.get("/dashboard", follow_redirects=False)).status_code == 200   # still signed in

    await client.get("/logout")
    bad = await client.post("/login", data={"email": old_email, "password": PASSWORD}, follow_redirects=False)
    assert bad.status_code == 401
    good = await client.post("/login", data={"email": new_email, "password": PASSWORD}, follow_redirects=False)
    assert good.status_code == 302 and good.headers["location"] == "/dashboard"


async def test_duplicate_email_is_rejected(client: AsyncClient, db: AsyncSession, seed_tiers):
    user, other = await registered_recipient(db), await registered_recipient(db)
    original = user.email
    with logged_in(user):
        response = await client.post("/profile", data=details(user, email=other.email.upper()))
    assert response.status_code == 400
    assert "Another account already uses this email address." in response.text
    assert (await reloaded(db, user)).email == original


async def test_duplicate_mobile_is_rejected(client: AsyncClient, db: AsyncSession, seed_tiers):
    user, other = await registered_recipient(db), await registered_recipient(db)
    original = user.mobile
    with logged_in(user):
        response = await client.post("/profile", data=details(user, mobile=other.mobile))
    assert response.status_code == 400
    assert "Another account already uses this mobile number." in response.text
    assert (await reloaded(db, user)).mobile == original


@pytest.mark.parametrize("email", ["not-an-email", "name@example", "two words@example.com", "a@@example.com", ""])
async def test_invalid_email_is_rejected(client: AsyncClient, db: AsyncSession, seed_tiers, email):
    user = await registered_recipient(db)
    original = user.email
    with logged_in(user):
        response = await client.post("/profile", data=details(user, email=email))
    assert response.status_code == 400 and EMAIL_INVALID in response.text
    assert f'value="{email}"' in response.text               # what they typed is kept
    assert (await reloaded(db, user)).email == original


@pytest.mark.parametrize("mobile", ["12345", "+27 82 111 2233", "phone", "+2782111223344556"])
async def test_invalid_mobile_is_rejected(client: AsyncClient, db: AsyncSession, seed_tiers, mobile):
    user = await registered_recipient(db)
    original = user.mobile
    with logged_in(user):
        response = await client.post("/profile", data=details(user, mobile=mobile))
    assert response.status_code == 400 and MOBILE_INVALID in response.text
    assert (await reloaded(db, user)).mobile == original


async def test_an_older_format_mobile_does_not_block_an_email_change(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    user.mobile = f"+27 82 {uuid.uuid4().int % 1000:03d} {uuid.uuid4().int % 10000:04d}"   # pre-validation style
    await db.commit()
    await db.refresh(user)
    legacy_mobile, new_email = user.mobile, unique_email("legacy")
    with logged_in(user):
        ok = await client.post("/profile", data=details(user, email=new_email), follow_redirects=False)
        assert ok.status_code == 302
        # Changing the mobile itself still has to meet the format.
        bad = await client.post("/profile", data=details(user, mobile="+27 82 000 0000"))
    assert bad.status_code == 400 and MOBILE_INVALID in bad.text
    user = await reloaded(db, user)
    assert user.email == new_email and user.mobile == legacy_mobile


async def test_an_older_format_email_does_not_block_a_mobile_change(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    user.email = f"legacy{uuid.uuid4().hex[:8]}@localhost"                               # no TLD
    await db.commit()
    await db.refresh(user)
    mobile = unique_mobile()
    with logged_in(user):
        ok = await client.post("/profile", data=details(user, mobile=mobile), follow_redirects=False)
    assert ok.status_code == 302 and (await reloaded(db, user)).mobile == mobile


async def test_no_changes_is_not_an_error(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        await client.post("/profile", data=details(user, full_name=user.full_name))
        page = (await client.get("/profile")).text
    assert "No changes to save." in page


# ── Full name ─────────────────────────────────────────────────────────────────

async def test_name_is_editable_before_kyc(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        page = (await client.get("/profile/edit")).text
        assert 'name="full_name"' in page
        response = await client.post("/profile", data=details(user, full_name="  Lerato Mokoena "), follow_redirects=False)
    assert response.status_code == 302
    assert (await reloaded(db, user)).full_name == "Lerato Mokoena"


async def test_blank_name_is_rejected(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    original = user.full_name
    with logged_in(user):
        response = await client.post("/profile", data=details(user, full_name="   "))
    assert response.status_code == 400 and "Enter your full name." in response.text
    assert (await reloaded(db, user)).full_name == original


@pytest.mark.parametrize("status", list(KYCSubmissionStatus))
async def test_name_is_locked_once_kyc_is_submitted(client: AsyncClient, db: AsyncSession, seed_tiers, status):
    user = await registered_recipient(db)
    await a_submission(db, user, status)
    original = user.full_name
    with logged_in(user):
        page = (await client.get("/profile/edit")).text
        assert 'name="full_name"' not in page                       # shown read-only
        assert "Your name is locked because you've submitted identity verification." in page
        assert "re-verification" not in page
        # The server enforces it even if a crafted request posts a new name.
        response = await client.post("/profile", data=details(user, full_name="Someone Else"))
        assert response.status_code == 400 and NAME_LOCKED.replace("'", "&#39;") in response.text
        # Email and mobile still change; resending the unchanged name is fine.
        new_email = unique_email("locked.name")
        ok = await client.post("/profile", data=details(user, full_name=original, email=new_email),
                               follow_redirects=False)
    assert ok.status_code == 302
    user = await reloaded(db, user)
    assert user.full_name == original and user.email == new_email


async def test_profile_edit_never_changes_the_kyc_submission(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    sub = await a_submission(db, user, KYCSubmissionStatus.approved)
    user_id = user.id
    columns = [c.key for c in KYCSubmission.__table__.columns]
    before = {c: getattr(sub, c) for c in columns}
    with logged_in(user):
        new_email = unique_email("after.kyc")
        edited = await client.post("/profile", data=details(user, email=new_email, mobile=unique_mobile()),
                                   follow_redirects=False)
        assert edited.status_code == 302
        await client.post("/profile/password", data={"current_password": PASSWORD, "new_password": "NewPass5678!",
                                                     "confirm_password": "NewPass5678!"})
    db.expire_all()
    rows = (await db.execute(select(KYCSubmission).where(KYCSubmission.user_id == user_id))).scalars().all()
    assert len(rows) == 1
    assert {c: getattr(rows[0], c) for c in columns} == before
    assert rows[0].email != new_email


# ── Admins ────────────────────────────────────────────────────────────────────

async def test_admin_edits_email_mobile_and_password(client: AsyncClient, db: AsyncSession):
    admin = await an_admin(db)
    original_name, email, mobile = admin.full_name, unique_email("ops"), unique_mobile()
    with logged_in(admin):
        page = (await client.get("/profile/edit")).text
        assert 'action="/profile"' in page and 'name="full_name"' not in page
        assert 'action="/profile/password"' in (await client.get("/profile/password")).text
        r1 = await client.post("/profile", data=details(admin, email=email, mobile=mobile),
                               follow_redirects=False)
        r2 = await client.post("/profile/password", data={"current_password": PASSWORD, "new_password": "Admin9876!",
                                                          "confirm_password": "Admin9876!"}, follow_redirects=False)
        r3 = await client.post("/profile", data=details(admin, full_name="Renamed Admin"))
    assert r1.status_code == 302 and r2.status_code == 302 and r3.status_code == 400
    admin = await reloaded(db, admin)
    assert (admin.email, admin.mobile, admin.full_name) == (email, mobile, original_name)
    assert verify_password("Admin9876!", admin.password_hash)


# ── Password ──────────────────────────────────────────────────────────────────

def pw(current=PASSWORD, new="NewPass5678!", confirm=None):
    return {"current_password": current, "new_password": new, "confirm_password": new if confirm is None else confirm}


async def test_password_change(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        response = await client.post("/profile/password", data=pw(), follow_redirects=False)
        assert response.status_code == 302
        page = (await client.get("/profile")).text
    assert "Your password has been changed." in page
    user = await reloaded(db, user)
    assert verify_password("NewPass5678!", user.password_hash) and not verify_password(PASSWORD, user.password_hash)


@pytest.mark.parametrize("data, message", [
    (pw(current="WrongPass1!"), "Your current password is incorrect."),
    (pw(confirm="NewPass0000!"), "Passwords do not match."),
    (pw(new="short1!"), "Password must be at least 8 characters."),
    (pw(current=""), "Your current password is incorrect."),
])
async def test_password_change_is_refused(client: AsyncClient, db: AsyncSession, seed_tiers, data, message):
    user = await registered_recipient(db)
    old_hash = user.password_hash
    with logged_in(user):
        response = await client.post("/profile/password", data=data)
    assert response.status_code == 400 and message in response.text
    for value in set(data.values()) - {""}:                      # never echoed back
        assert value not in response.text
    assert (await reloaded(db, user)).password_hash == old_hash


async def test_password_inputs_never_carry_a_value(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await registered_recipient(db)
    with logged_in(user):
        page = (await client.post("/profile/password", data=pw(confirm="Mismatch000!"))).text
    for field in re.findall(r'<input type="password"[^>]*>', page):
        assert "value=" not in field


def test_the_lock_message_drops_the_re_verification_sentence():
    assert NAME_LOCKED == "Your name can't be changed after you've submitted identity verification."


# ── Registration uses the same format rules ──────────────────────────────────

def registration(**overrides):
    data = {"full_name": "Format Check", "email": unique_email("format"), "mobile": unique_mobile(),
            "password": PASSWORD, "confirm_password": PASSWORD}
    data.update(overrides)
    return data


@pytest.mark.parametrize("field, value, message", [
    ("email", "not-an-email", EMAIL_INVALID),
    ("email", "name@example", EMAIL_INVALID),
    ("mobile", "+27 82 111 2233", MOBILE_INVALID),
    ("mobile", "12345", MOBILE_INVALID),
])
async def test_registration_rejects_bad_formats(client: AsyncClient, db: AsyncSession, field, value, message):
    data = registration(**{field: value})
    response = await client.post("/register", data=data, follow_redirects=False)
    assert response.status_code == 400 and message.replace("'", "&#39;") in response.text
    taken = await db.execute(select(User).where(User.email == data["email"].lower()))
    assert taken.scalar_one_or_none() is None


async def test_registration_still_reports_password_mismatch(client: AsyncClient):
    response = await client.post("/register", data=registration(confirm_password="Different1!"))
    assert response.status_code == 400 and "do not match" in response.text


async def test_registration_with_good_formats_succeeds(client: AsyncClient):
    response = await client.post("/register", data=registration(email="  Mixed.Case." + uuid.uuid4().hex[:6] + "@Example.com "),
                                 follow_redirects=False)
    assert response.status_code == 302 and response.headers["location"] == "/dashboard"
