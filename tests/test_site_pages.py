"""UI redesign 6c: landing routing, the site footer, its public pages and the scroll reveal."""
import json
import re
import uuid
from base64 import b64encode
from pathlib import Path

import pytest
from httpx import AsyncClient
from itsdangerous import TimestampSigner
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from tests.remit_helpers import approved_sender, logged_in
from tests.test_public_pages import sentence_em_dashes, undecorated_icons, visible_text
from tests.test_shell import an_admin

INFO_PAGES = {
    "/about": "About us",
    "/contact": "Contact us",
    "/help": "Help and support",
    "/legal/data-security": "Data security",
    "/legal/information-security": "Information security",
    "/legal/terms": "Terms and conditions",
    "/legal/privacy": "Privacy policy",
}
FOOTER_NOTE = "Academic prototype. Runs on the XRPL Testnet; no real money moves."
CSS = Path("frontend/static/css/design-system.css").read_text()
JS = Path("frontend/static/js/ui.js").read_text()


def session_cookie(data: dict) -> str:
    """A cookie exactly as SessionMiddleware would sign it."""
    return TimestampSigner(str(settings.secret_key)).sign(b64encode(json.dumps(data).encode())).decode()


# ── 6.1 Routing: signed-out visitors always get the landing page ─────────────

async def test_fresh_visitor_gets_the_landing_page_not_login(client: AsyncClient):
    client.cookies.clear()
    response = await client.get("/", follow_redirects=False)
    assert response.status_code == 200
    assert "location" not in response.headers
    assert 'id="landing-title"' in response.text


@pytest.mark.parametrize("session", [
    {"user_id": str(uuid.uuid4())},       # a user that no longer exists
    {"user_id": "not-a-uuid"},            # a malformed session
    {"flash": {"message": "Hi", "kind": "info"}},   # a session with no user
])
async def test_stale_sessions_still_get_the_landing_page(client: AsyncClient, session):
    client.cookies.set("session", session_cookie(session))
    response = await client.get("/", follow_redirects=False)
    client.cookies.clear()
    assert response.status_code == 200 and 'id="landing-title"' in response.text


async def test_after_logout_the_landing_page_shows(client: AsyncClient, db: AsyncSession, seed_tiers):
    user = await approved_sender(db)
    await client.post("/login", data={"email": user.email, "password": "Pass1234!"})
    assert (await client.get("/", follow_redirects=False)).headers["location"] == "/dashboard"
    await client.get("/logout")
    assert (await client.get("/", follow_redirects=False)).status_code == 200


# ── 6.2 Hero label ────────────────────────────────────────────────────────────

async def test_hero_has_no_prototype_badge(client: AsyncClient):
    page = (await client.get("/")).text
    assert "XRPL Testnet prototype" not in page
    assert "ds-landing-eyebrow" not in page and "ds-landing-eyebrow" not in CSS


# ── 6.3 Scroll reveal ─────────────────────────────────────────────────────────

async def test_landing_sections_are_marked_for_reveal_but_rendered_visible(client: AsyncClient):
    page = (await client.get("/")).text
    assert page.count("data-reveal>") == 3            # how it works, real vs simulated, CTA band
    assert page.count("data-reveal-item") >= 9
    # The server never renders the hidden state: only ui.js adds it.
    assert "ds-reveal-pending" not in page and "ds-reveal-ready" not in page


def test_reveal_css_hides_only_once_js_is_ready_and_never_under_reduced_motion():
    hides = re.findall(r"([^{}]*)\{[^{}]*opacity:\s*0;", re.sub(r"/\*.*?\*/", "", CSS, flags=re.S))
    pending = [sel.strip() for sel in hides if "ds-reveal-pending" in sel]
    assert pending and all(sel.startswith("html.ds-reveal-ready") for sel in pending)
    reduced = CSS[CSS.index("@media (prefers-reduced-motion: reduce) {\n  html.ds-reveal-ready"):]
    assert "opacity: 1 !important" in reduced[:200]
    assert "html.ds-reduce-motion.ds-reveal-ready .ds-reveal-pending { opacity: 1 !important" in CSS
    # Only transform and opacity animate, on the existing motion tokens.
    rule = re.search(r"\.ds-reveal-in \{(.*?)\}", CSS, re.S).group(1)
    assert set(re.findall(r"(\w+) calc\(var\(--dur-slow\)", rule)) == {"opacity", "transform"}
    assert "var(--ease-out)" in rule


def test_reveal_js_is_guarded():
    body = JS[JS.index("function initReveal"):JS.index("// ── Auto-wiring")]
    assert "reducedMotion()" in body and '"IntersectionObserver" in window' in body
    assert "getBoundingClientRect().top < window.innerHeight" in body     # on-screen content stays put
    assert body.index('classList.add("ds-reveal-ready")') > body.index("revealObserver.observe")
    assert "catch" in body and "revealAll()" in body
    assert "unobserve" in JS[JS.index("function revealItem"):JS.index("function revealAll")]   # once


# ── 6.4 Footer and its pages ─────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", *INFO_PAGES])
async def test_footer_on_landing_and_info_pages(client: AsyncClient, path):
    page = (await client.get(path)).text
    footer = page[page.index('<footer class="ds-site-footer">'):page.index("</footer>")]
    for heading in ("About us", "Contact us", "Legal", "Help and support"):
        assert f">{heading}</h2>" in footer
    for href in INFO_PAGES:
        assert f'href="{href}"' in footer
    for label in ("Data security", "Information security", "Terms and conditions", "Privacy policy"):
        assert f">{label}</a>" in footer
    assert FOOTER_NOTE in footer
    assert not undecorated_icons(footer)


@pytest.mark.parametrize("path", ["/login", "/register", "/no-such-page"])
async def test_no_footer_on_auth_and_error_pages(client: AsyncClient, path):
    response = await client.get(path, headers={"accept": "text/html"})
    assert "<footer" not in response.text and FOOTER_NOTE not in response.text


def test_footer_is_a_separate_band_and_links_hover_by_colour_only():
    rule = re.search(r"\.ds-site-footer \{(.*?)\}", CSS, re.S).group(1)
    assert "background: var(--chalk-soft)" in rule
    hover = re.search(r"\.ds-site-footer a:not\(\.ds-wordmark\):hover \{(.*?)\}", CSS).group(1)
    assert "color: var(--accent)" in hover and "text-decoration" not in hover
    assert "text-decoration: none" in re.search(r"\.ds-site-footer a:not\(\.ds-wordmark\) \{(.*?)\}", CSS).group(1)


# ── Follow-ups: fold, register steps, auth links ──────────────────────────────

def test_hero_fills_the_first_screen_so_how_it_works_starts_below_the_fold():
    rule = re.search(r"\.ds-landing-hero \{(.*?)\}", CSS, re.S).group(1)
    assert "min-height: calc(100svh - var(--landing-hero-top))" in rule
    assert "min-height: calc(100vh - var(--landing-hero-top))" in rule      # fallback


async def test_register_steps_say_system_approval(client: AsyncClient):
    page = (await client.get("/register")).text
    assert "System approval" in page and "Our team reviews your details." in page
    assert "Admin approval" not in page and "An admin reviews your details." not in page


@pytest.mark.parametrize("path, href, label", [("/login", "/register", "Register"), ("/register", "/login", "Log in")])
async def test_auth_switch_links_are_berry_semibold_without_underline(client: AsyncClient, path, href, label):
    page = (await client.get(path)).text
    assert re.search(rf'<p class="ds-auth__switch">[^<]*<a href="{href}">{label}</a></p>', page)
    link = re.search(r"\.ds-auth__switch a \{(.*?)\}", CSS).group(1)
    assert "color: var(--accent)" in link and "text-decoration: none" in link
    assert "font-weight: 600" in link        # a non-colour cue (WCAG 1.4.1)
    assert "text-decoration: none" in re.search(r"\.ds-auth__switch a:hover \{(.*?)\}", CSS).group(1)


async def test_footer_is_not_in_the_app_shell(client: AsyncClient, db: AsyncSession, seed_tiers):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/dashboard")).text
    assert "ds-site-footer" not in page


@pytest.mark.parametrize("path, title", INFO_PAGES.items())
async def test_info_pages_are_public(client: AsyncClient, path, title):
    client.cookies.clear()
    response = await client.get(path, follow_redirects=False)
    assert response.status_code == 200
    page = response.text
    assert f"<h1>{title}</h1>" in page and page.count("<h1") == 1
    assert f"<title>{title} · XRPL Remit</title>" in page
    assert "ds-sidebar" not in page                               # signed-out layout
    assert 'href="/login"' in page and 'href="/register"' in page
    assert "academic prototype" in page and FOOTER_NOTE in page
    assert not undecorated_icons(page)
    assert not sentence_em_dashes(page)


@pytest.mark.parametrize("path", INFO_PAGES)
async def test_info_pages_invent_no_contact_or_licence_details(client: AsyncClient, path):
    page = (await client.get(path)).text
    text = visible_text(page)
    assert not re.search(r"\+?\d[\d ()-]{6,}\d", text), "no phone, licence or registration numbers"
    assert not re.search(r"\b(FSP|FSCA|Reg\.? ?No|registration number|licen[cs]e number)\b", text, re.I)
    addresses = re.findall(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text) + re.findall(r'href="mailto:([^"]+)"', page)
    assert addresses and all(a.endswith("@example.com") for a in addresses), addresses


async def test_legal_pages_link_to_each_other(client: AsyncClient):
    page = (await client.get("/legal/privacy")).text
    related = page[page.index('aria-label="Legal pages"'):]
    assert '<a href="/legal/privacy" aria-current="page">' in related
    for slug in ("data-security", "information-security", "terms"):
        assert f'href="/legal/{slug}"' in related


async def test_unknown_legal_page_is_a_404(client: AsyncClient):
    assert (await client.get("/legal/cookies")).status_code == 404


async def test_signed_in_visitors_keep_the_public_layout_with_a_way_back(client: AsyncClient, db: AsyncSession, seed_tiers):
    with logged_in(await approved_sender(db)):
        page = (await client.get("/help")).text
    assert "ds-sidebar" not in page and 'href="/dashboard"' in page and "Go to your dashboard" in page
    with logged_in(await an_admin(db)):
        page = (await client.get("/legal/terms")).text
    assert 'href="/admin"' in page and "Go to the overview" in page
