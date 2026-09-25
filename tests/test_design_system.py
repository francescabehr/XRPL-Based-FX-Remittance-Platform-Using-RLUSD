"""Design-system foundation (UI redesign Phase 2): component macros and the dev styleguide."""
import re
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.config import settings
from app.models.cashout import CashOutStatus
from app.models.transaction import CashInStatus, SettlementStatus
from app.models.user import KYCStatus
from app.routers import dev
from app.templating import make_templates

env = make_templates().env

IMPORTS = (
    '{% from "components/status.html" import STATUS, status_badge, transaction_badge, cashout_badge %}'
    '{% from "components/money.html" import amount %}'
    '{% from "components/ui.html" import SEND_STEPS, stepper, empty_state %}'
)


def render(source: str, **context) -> str:
    return env.from_string(IMPORTS + source).render(**context)


def status_table() -> dict:
    return env.get_template("components/status.html").module.STATUS


# ── Status badges ─────────────────────────────────────────────────────────────

def test_status_table_covers_every_model_enum():
    """The badge map is keyed by the real enum values — none may be missing."""
    table = status_table()
    assert set(table["kyc"]) == {s.value for s in KYCStatus}
    assert set(table["cashin"]) == {s.value for s in CashInStatus}
    assert set(table["settlement"]) == {s.value for s in SettlementStatus}
    assert {s.value for s in CashOutStatus} <= set(table["cashout"])


@pytest.mark.parametrize(
    "domain, value, label, tone",
    [
        ("kyc", "not_submitted", "Not started", "neutral"),
        ("kyc", "pending", "Awaiting approval", "warning"),
        ("kyc", "approved", "Verified", "success"),
        ("kyc", "rejected", "Rejected", "danger"),
        ("cashin", "pending", "Awaiting payment", "warning"),
        ("cashin", "received", "Payment received", "success"),
        ("cashin", "failed", "Failed", "danger"),
        ("settlement", "not_queued", "Not started", "neutral"),
        ("settlement", "queued", "Queued", "warning"),
        ("settlement", "processing", "Settling on XRPL…", "warning"),
        ("settlement", "completed", "Settled on XRPL", "success"),
        ("settlement", "failed", "Failed", "danger"),
        ("cashout", "requested", "Requested", "warning"),
        ("cashout", "approved", "Approved", "warning"),
        ("cashout", "awaiting_ledger", "Awaiting ledger confirmation", "warning"),
        ("cashout", "completed", "Completed", "success"),
        ("cashout", "failed", "Failed", "danger"),
    ],
)
def test_status_badge_label_and_tone(domain, value, label, tone):
    html = render("{{ status_badge(value, domain) }}", value=value, domain=domain)
    assert f"ds-status--{tone}" in html
    assert label in html  # colour is never the only signal


def test_status_badge_accepts_enum_members():
    html = render('{{ status_badge(s, "kyc") }}', s=KYCStatus.approved)
    assert "Verified" in html and 'data-status="approved"' in html


def test_only_in_progress_states_pulse():
    assert "ds-pulse-dot" in render('{{ status_badge("processing", "settlement") }}')
    assert "ds-pulse-dot" not in render('{{ status_badge("queued", "settlement") }}')
    assert "ds-pulse-dot" not in render('{{ status_badge("completed", "settlement") }}')


def test_unknown_status_renders_neutral_instead_of_raising():
    html = render('{{ status_badge("something_new", "settlement") }}')
    assert "ds-status--neutral" in html and "Something new" in html


@pytest.mark.parametrize(
    "cashin, settlement, label",
    [
        (CashInStatus.failed, SettlementStatus.not_queued, "Failed"),
        (CashInStatus.pending, SettlementStatus.not_queued, "Awaiting payment"),
        (CashInStatus.received, SettlementStatus.processing, "Settling on XRPL…"),
        (CashInStatus.received, SettlementStatus.completed, "Settled on XRPL"),
    ],
)
def test_transaction_badge_derives_one_overall_state(cashin, settlement, label):
    txn = SimpleNamespace(cashin_status=cashin, settlement_status=settlement)
    assert label in render("{{ transaction_badge(txn) }}", txn=txn)


def test_cashout_badge_shows_awaiting_ledger_confirmation_verbatim():
    req = SimpleNamespace(status=CashOutStatus.approved, awaiting_ledger_confirmation=True)
    assert "Awaiting ledger confirmation" in render("{{ cashout_badge(req) }}", req=req)
    req.awaiting_ledger_confirmation = False
    assert ">Approved<" in render("{{ cashout_badge(req) }}", req=req)


# ── Amounts ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value, currency, kwargs, text",
    [
        (Decimal("50.874404"), "UCTUSD", "", "50.874404 UCTUSD"),
        (Decimal("1000"), "ZAR", "", "R1,000.00"),
        (Decimal("1000"), "USD", "", "$1,000.00"),
        (Decimal("-1000"), "ZAR", "", "−R1,000.00"),
        (Decimal("50.874404"), "UCTUSD", ", signed=true", "+50.874404 UCTUSD"),
        (Decimal("166.5"), "USD", ", decimals=6", "$166.500000"),
    ],
)
def test_amount_matches_app_formats_in_one_text_node(value, currency, kwargs, text):
    html = render(f"{{{{ amount(v, c{kwargs}) }}}}", v=value, c=currency)
    # Symbol/code and number share one text node: no tags between them.
    assert f">{text}<" in html


def test_amount_count_up_carries_prefix_suffix_and_decimals():
    zar = render('{{ amount(v, "ZAR", count_up=true) }}', v=Decimal("1000"))
    assert 'data-prefix="R"' in zar and 'data-suffix=""' in zar and 'data-decimals="2"' in zar
    uct = render('{{ amount(v, "UCTUSD", count_up=true) }}', v=Decimal("50.874404"))
    assert 'data-prefix=""' in uct and 'data-suffix=" UCTUSD"' in uct and 'data-decimals="6"' in uct


def test_amount_accepts_currency_enums_and_none():
    currency = SimpleNamespace(value="USD")
    assert ">$12.50<" in render("{{ amount(v, c) }}", v=Decimal("12.5"), c=currency)
    assert "—" in render('{{ amount(none, "ZAR") }}')


# ── Layout components ─────────────────────────────────────────────────────────

def test_stepper_marks_current_and_completed_steps():
    html = render("{{ stepper(SEND_STEPS, 2) }}")
    current = re.search(r'<li[^>]*aria-current="step"[^>]*>(.*?)</li>', html, re.S).group(1)
    assert "Review" in current
    assert html.count("(completed)") == 1  # only "Details" is done
    assert all(label in html for label in ["Details", "Review", "Pay", "Status"])


def test_empty_state_renders_action_link():
    html = render('{{ empty_state("No transfers yet", "Send money to start.", {"label": "Send money", "href": "/send"}) }}')
    assert "No transfers yet" in html and 'href="/send"' in html and "Send money" in html


# ── /dev/styleguide ───────────────────────────────────────────────────────────

async def test_styleguide_not_registered_without_debug():
    from app.main import app

    assert not settings.debug
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/dev/styleguide", follow_redirects=False)
    assert response.status_code == 404


async def test_styleguide_renders_every_component_without_secrets():
    debug_app = FastAPI()
    debug_app.include_router(dev.router)
    async with AsyncClient(transport=ASGITransport(app=debug_app), base_url="http://test") as client:
        response = await client.get("/dev/styleguide")
    assert response.status_code == 200
    page = response.text

    assert "/static/css/design-system.css" in page
    assert "/static/js/ui.js" in page
    # Importing the `flash` macro must not shadow base.html's `flash` variable.
    assert 'class="alert alert- ' not in page
    for values in status_table().values():
        for label, _tone, _pulse in values.values():
            assert label in page

    secrets = [settings.secret_key, settings.xrpl_platform_wallet_seed, settings.xrpl_encryption_key]
    for secret in filter(None, secrets):
        assert secret not in page
