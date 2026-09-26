# UI Redesign Brief

This brief is for Claude Code. It explains how to redesign the front end of our XRPL remittance platform. Read the whole brief before changing anything, then follow the phases in order.

## 0. Context

- **App:** ECO5040W class project, a cross-border remittance prototype. A sender in South Africa pays ZAR, and the recipient receives UCTUSD, a test IOU on the XRPL Testnet, in a custodial web wallet. The recipient can then request a simulated fiat cash-out.
- **Stack:** Python (FastAPI) back end with server-rendered Jinja templates under `frontend/templates/`, extending `base.html`, and PostgreSQL. Static files live in `frontend/static/` and are served at `/static/`. Styling is **Bootstrap 5.3 (CDN), skinned** by `frontend/static/css/design-system.css`.
- **Scope:** Visual and UX only. Do not change routes, form field names, business logic, fee or FX calculations, or database models unless a phase below says so explicitly. All existing tests must keep passing.
- **Audience for the result:** Lecturers assessing a live demo. The demo path needs to look polished, clear and trustworthy, like a real fintech product.

## 1. Design references

Reference screenshots are in the `design_references/` folder in the project root. Open every image in it before designing. If a file name differs from the table below, match the image to its row by what it shows.

| File | Product | What to take from it |
|---|---|---|
| `wise-home.png` | Wise | Dashboard layout. Total balance shown large, primary actions as pills (Send / Add money), a clean sidebar, and recent transactions below. |
| `wise-send-amount.png` | Wise | A send flow as a stepper (Recipient → Amount → Review → Pay). A huge "they receive exactly X" figure and a line-by-line breakdown with icons. |
| `revolut-transaction.png` | Revolut | A transaction list, plus a right-hand **detail drawer** listing status, fee, amount bought and exchange rate. Failed items are struck through and carry a red marker. |
| `stripe-dashboard.png` | Stripe | A data-dense admin overview. Summary tiles, a coloured status breakdown, a "Failed payments" list with red pills, and small charts. |
| `stripe-landing.png` | Stripe | Bold marketing hero typography. Use it only as inspiration for the landing page. |
| `uvodo-landing.png` | Uvodo | A friendly landing hero with floating "product" cards. |
| `uvodo-onboarding.png` | Uvodo | A "Let's get you started" checklist card with a progress bar and completed and pending steps. |
| `base-processing.png` | Base | A calm, centred "Processing — do not close this window" state. Use it for the **"Settling on XRPL…"** state (queued/processing settlements). |

**Rules for using the references:**
- Borrow **patterns and layout ideas only**.
- Never copy logos, product names, illustrations, wordmarks or exact brand colours.
- Our product must look like its own brand.

## 2. Design direction

The look should be calm, confident and precise: a trustworthy money app, not a crypto casino.

- **Background:** Near-white app background (`--bg` #F7F7FA), with white cards (`--surface` #FFFFFF) on top separated by very subtle borders or shadows.
- **Palette** (deep teal brand, bubblegum-pink highlight, lavender soft accent; tokens in `design-system.css`):

  | Role | Token(s) | Value | Use |
  |---|---|---|---|
  | Brand / primary | `--accent`, `--accent-hover`, `--accent-active`, `--accent-soft` | deep teal #1B4B4F, #143A3D, #0F2D2F, wash #E6EEEE | Buttons, links, heading accents, key amounts. White text on it is 9.7:1. |
| Mid-tone teal | `--accent-mid` | #069494 | Secondary elements: icon circles/glyphs, chart lines. 3.7:1 on white, so fine for icons and graphics but **not body text**. |
  | Highlight | `--highlight`, `--highlight-hover`, `--highlight-soft`, `--highlight-ink` | bubblegum pink #FF69B4, #FC5AAB, #FFE3F1; ink #102E31 | **Fills only, never text on white** (2.65:1): progress bars, active nav item, check-draw, focus halo, the "Recipient gets" hero row. Text on pink is the very dark teal `--highlight-ink` (5.4:1 on pink, 4.9:1 on hover). Pink sits on or next to teal so the shape keeps its contrast (the progress track is teal: 3.7:1). **Pink is never a status colour**: danger stays red (#F04438, hue 4° vs pink's 330°), and pink never appears in badges, alerts or error states. |
  | Soft accent | `--lavender`, `--lavender-soft`, `--lavender-ink` | #D9C8F5, tint #EDE4FB, ink #4A2E86 | Secondary buttons, avatar backgrounds, soft card highlights, empty-state icons. Text on it is lavender-ink (8.4:1 on the tint). |
  | Status (separate from brand) | `--{success,warning,danger,info}-vivid` / `-soft` / base | success #12B76A / #ECFDF3 / #027A48 · warning #F79009 / #FFFAEB / #B54708 · danger #F04438 / #FEF3F2 / #B42318 · info #2E90FA / #EFF8FF / #175CD3 | Three tones: **vivid** for dots and bars (always beside a text label), **tint** for backgrounds, **deep** for text (≥ 4.5:1 on tint and on white). |
  | Neutral | `--neutral`, `--neutral-soft`, `--text`, `--text-muted`, `--border`, `--border-input` | #5B6071, #EFF0F4, #16161D, #5B6071, #E6E6EE, #868B9C | Grey badges, body text, muted text, hairlines, form-control borders (3.4:1). |

  Focus rings are a 2px deep-teal outline with a pink halo, so the teal carries the contrast. Semantic colours are reserved for status and carry meaning, not decoration. Every text/background pair is checked for WCAG AA.
- **Money:** Money is the hero. Amounts are large and semibold, use tabular numerals, and always show the currency code (ZAR, UCTUSD, USD).
- **Shape:** Generous whitespace, rounded corners (roughly 12–16px on cards) and pill-shaped buttons.
- **Font:** One typeface. Use Inter from Google Fonts, with `font-feature-settings: "tnum"` on amounts.
- **Layout:** Desktop-first, fully usable down to phone width. On desktop, a left sidebar holds the navigation; on mobile it collapses to a top bar with a menu.
- **Tone of copy:** Short and human, e.g. "Recipient gets", "Awaiting approval", "Settled on XRPL".

## 3. Phase 1: Audit first, change nothing

Before any edits:
1. **Templates:** List every template, the route that renders it, and which role sees it (sender, recipient, admin, public).
2. **Styling:** Record how styling currently works: inline styles, a CSS file, a framework such as Bootstrap or Tailwind, or a mix.
3. **Components:** Identify repeated UI pieces that should become shared macros or partials, such as buttons, cards, status badges, tables, forms, flash messages and amount displays.
4. **Navigation:** Flag dead links and inconsistencies. (The Phase 1 audit found no dead links: `/admin/cashout` and `/admin/transactions` are live routes. See UI_AUDIT.md §4.)
5. **Plan:** Propose which screens fall under the demo path (Phase 4) and which are secondary (Phase 5).

Write the audit to `UI_AUDIT.md` in the project root, then stop and wait for approval.

## 4. Phase 2: Design system foundation

Build `frontend/static/css/design-system.css` (served at `/static/css/design-system.css`, loaded after Bootstrap in `base.html`). **Skin Bootstrap, don't replace it:** map the tokens onto Bootstrap's CSS variables and theme its own `.btn`, `.card`, `.badge`, `.alert`, `.table` and form classes in place. Keep the grid, collapse and dropdown JS. Anything Bootstrap doesn't have gets a new class prefixed `ds-`. It should define:

- **CSS custom properties on `:root`:**
  - colour tokens: `--bg`, `--surface`, `--border`, `--text`, `--text-muted`, `--accent`, `--accent-hover`, `--accent-soft`, `--success`, `--warning`, `--danger`, `--info`, each with a `-soft` background variant;
  - a type scale, including `--text-amount-xl` for hero amounts;
  - a spacing scale on a 4px base;
  - radius values;
  - one or two shadows.
- **Base element styles:** body, headings, links, form inputs, selects, labels, focus rings (visible and accessible), and tables.
- **Component classes:** themed Bootstrap `.btn-primary` / `.btn-outline-secondary` (secondary) / `.btn-danger`, plus `.ds-btn-ghost`; themed `.card`; `.badge` with a `.ds-status--{tone}` modifier; and the new `.ds-amount`, `.ds-stat-tile`, `.ds-progress`, `.ds-stepper`, `.ds-breakdown`, `.ds-drawer`, `.ds-empty-state`, `.ds-flash`, `.ds-card-link`.

Also create Jinja macros in `frontend/templates/components/` (`status.html`, `money.html`, `ui.html`, `xrpl.html`):
- `status_badge(status)`: a single source of truth that maps each status to a colour and label (see section 6).
- `amount(value, currency, size)`: matches the app's existing formats: `R1,000.00` (ZAR), `$1,000.00` (USD), `50.874404 UCTUSD` (UCTUSD, 6dp). The symbol/code and the number are always in one text node.
- `stat_tile(label, value, sublabel)`
- `stepper(steps, current)`
- `empty_state(title, message, action)`

Finally, add a hidden dev-only page (e.g. `/dev/styleguide`, registered only in debug mode) that renders every component, so the look can be reviewed in one place.

**Accessibility:** WCAG AA contrast, visible keyboard focus, and labels on every input. Colour must never be the only signal; status badges always include text.

### Motion foundation

Motion must explain what is happening or confirm that something worked. It is never decoration, and it never blocks the user.

**Tokens.** Add these to `:root`:
- `--dur-fast: 120ms` for hovers, presses and focus.
- `--dur-base: 200ms` for most transitions.
- `--dur-slow: 320ms` for drawers, page-level reveals and the success check.
- `--ease-out: cubic-bezier(0.2, 0.8, 0.2, 1)` for elements entering.
- `--ease-in-out: cubic-bezier(0.4, 0, 0.2, 1)` for elements moving or changing state.

**Rules:**
- Animate only `transform` and `opacity`. Colour transitions are also fine on badges and buttons. Never animate `width`, `height`, `top` or `left`; use `transform: scaleX()` for progress bars.
- Nothing may delay an action. Forms submit immediately, and animations play alongside navigation, not before it.
- Loops (the pulse and the shimmer) are allowed only on things that are genuinely in progress.
- Everything is built in plain CSS (`transition` and `@keyframes`) plus small vanilla JS. Add no animation libraries.
- **Reduced motion is mandatory.** Wrap motion so that under `@media (prefers-reduced-motion: reduce)`:
  - all transition and animation durations drop to ~0ms;
  - loops stop;
  - count-ups show the final value immediately;
  - state changes still happen, just instantly.

**Utilities to build in `design-system.css`** (all prefixed `ds-`):
- `.ds-fade-in` and `.ds-slide-up`: small entrance animations, 8px of travel.
- `.ds-skeleton`: a grey block with a shimmer sweep, used as a loading placeholder.
- `.ds-pulse-dot`: a small pulsing dot for in-progress states.
- `.ds-check-draw`: an SVG check mark that draws itself with `stroke-dashoffset`.
- `.ds-drawer` open and close: slides in from the right, with a backdrop fading to ~40% opacity.
- `.ds-flash` enter and auto-dismiss:
  - slides in from the top;
  - auto-dismisses after 5s for success and info messages only;
  - errors stay until closed.
- Button and card hover and press:
  - buttons: darken on hover and scale to 0.98 on press;
  - cards: a slight lift on hover (`translateY(-2px)` plus a stronger shadow), applied only to clickable cards.

**JS helpers.** Add a small `frontend/static/js/ui.js`, loaded with `defer`:
- `countUp(el, to, {duration})`: animates a number to a new value with tabular numerals and correct decimal places. It respects reduced motion.
- `copyToClipboard(btn)`: swaps the label to "Copied" for 1.5s.
- Drawer open and close:
  - traps focus inside the drawer and closes on Esc;
  - returns focus to the element that opened it.
- A polling helper for settlement status (see 7.4).

Add a **Motion** section to `/dev/styleguide` that demos every utility. It needs a toggle that simulates reduced motion.

## 5. Phase 3: App shell (✅ built)

`base.html` renders two layouts: the **signed-in shell** and a **signed-out** page (light header with the teal wordmark, content centred). There is no dark top navbar.

- **Sidebar** (`components/nav.html`, `sidebar(user, path)`):
  - deep-teal background (`--accent`), near-white text (#F4F8F8, 9.1:1), muted group labels (white at 72%, 5.9:1);
  - wordmark at the top ("XRPL Remit", pink mark with ink glyph);
  - role-aware grouped navigation with icons. The **active item is a pink pill (`--highlight`) with `--highlight-ink` text** and `aria-current="page"`. Inside the sidebar the focus ring is pink (a teal ring would vanish on teal);
  - non-admin: Home · **Send** (Send money, Beneficiaries, History — `can_send`) · **Wallet** (Wallet, Cash out, My cash-outs — `can_receive`, set once a wallet exists) · **Account** (Verification);
  - admin: Overview · **Queues** (KYC, Cash-in, Settlements, Cash-out) · **Monitor** (Transactions) · **Config** (Fees & limits);
  - footer: initials avatar (lavender-soft/lavender-ink), name, email (or "Administrator"), Log out;
  - **KYC status lives on the Verification item** as a small badge, and only when action is needed: "Not started" (grey), "Pending" (amber), "Rejected" (red). Verified users see no badge. Shown to users who can send (KYC gates sending); never to admins.
- **Mobile:** Bootstrap `offcanvas-lg`: a static sidebar at ≥992px, an off-canvas drawer below that, opened by the top bar's menu button (focus handling, Esc and backdrop come from Bootstrap).
- **Top bar:** sticky, light, and minimal: the page title (the page's `<h1>`) plus a back arrow where the page has one. No KYC badge. `page_actions` is used **only** when the page has no other way to do that action — today just Beneficiaries' "Add beneficiary". Dashboard actions live in its hero cards, Send money / Cash out in the sidebar, and admin counts or statuses in the page body.
- **Template blocks:** `page_title` (defaults to the `<title>` minus its suffix), `page_actions` (buttons/badges on the right) and `page_back` (an href). Page bodies no longer carry their own `<h3>` header row.
- **Flash messages:** `flash_message(...)` (`.ds-flash`). **Flash name clash:** import the macro under an alias, because a top-level name `flash` in a template shadows the `flash` context variable.
- **Confirmation:** forms for irreversible actions carry `data-confirm` (+ optional `data-confirm-title`, `-label`, `-tone`). `ui.js` asks in the shared `#ds-confirm` `<dialog>` and resubmits with the original button. Without JS the form submits directly (accepted for this prototype). Used on cash-in received/failed, cash-out approve/reject, settlement retry, KYC approve/reject and beneficiary delete.
- **Admin landing:** admins land on `/admin` (Overview) after login and when they open a user page.
- **Accessibility:** skip link to `#main`, landmarks (`nav`, `main`), labels tied to inputs, `aria-label` on icon-only buttons, decorative icons `aria-hidden`, disabled actions rendered as disabled buttons.

## 6. Status vocabulary

Every status is displayed through the macros in `frontend/templates/components/status.html` (`status_badge(status, domain)`, `transaction_badge(txn)`, `cashout_badge(req)`). The table below uses the **real enum values from the models** (approved in the Phase 1 review). Badges always include their text label.

| Domain | Enum values (code) | Label → colour |
|---|---|---|
| KYC (`users.kyc_status`) | not_submitted, pending, approved, rejected | Not started → grey; Awaiting approval → amber; Verified → green; Rejected → red |
| KYC submission (`kyc_submissions.status`) | pending, approved, rejected | as above |
| Cash-in (`transactions.cashin_status`) | pending, received, failed | Awaiting payment → amber; Payment received → green; Failed → red |
| Settlement (`transactions.settlement_status`) | not_queued, queued, processing, completed, failed | Not started → grey; Queued → amber; Settling on XRPL… → amber + pulse; Settled on XRPL → green; Failed → red |
| Transaction overall | derived: failed cash-in → Failed; pending cash-in → Awaiting payment; otherwise the settlement state | reuses the rows above |
| Cash-out (`cashout_requests.status`) | requested, approved, completed, failed, plus the derived **Awaiting ledger confirmation** (approved + burn hash + outcome unknown) | Requested → amber; Approved → amber + pulse; Awaiting ledger confirmation → amber; Completed → green; Failed → red |
| Beneficiary link | linked / not registered | Linked → green; Not registered → grey |
| AML flag | boolean | AML review → red |

The pulse dot marks only states that are genuinely in progress. Keep any text the tests assert on verbatim (e.g. "Awaiting ledger confirmation").

## 7. Phase 4: Demo-path screens (highest priority)

Redesign these screens in order. After each one, run the tests and check it at desktop and phone widths.

### 7.1 Onboarding and KYC (pattern: Uvodo checklist) — ✅ 4a

**Placement:** On the shared `/dashboard`, until the user is fully set up. The dashboard is **role-aware**: `can_send` users see the onboarding/KYC, limits and send content; `can_receive` users see a wallet-balance section with a **Cash out** button; dual-role users see both.

**Content:**
- A "Get started" card with a progress bar and three steps:
  1. Complete KYC.
  2. Admin approval.
  3. Add your first recipient.
- Completed steps show a check and muted text; pending steps show a clear call to action.
- If KYC is pending or rejected, explain what that means in one line, and say that sending is disabled until KYC is approved.

**KYC form:** Group fields into clear sections (Personal details, Address, Contact, Source of funds).

**Motion:**
- The progress bar fills to its current value on load, using `scaleX` over `--dur-slow`.
- A step that has just been completed shows `.ds-check-draw` once.

### 7.2 Sender dashboard (pattern: Wise home) — ✅ 4a

*As built (4a):* `/dashboard` is role-aware. A user with a wallet sees a **Wallet balance** hero (count-up, "available to cash out", Cash out / View wallet) **first** — money already held leads, as in Wise. `can_send` users then see the "Let's get you started" checklist until KYC is approved **and** a recipient exists, and — once KYC is approved — the "Available to send today" hero, "Used today / Used this month" tiles and Recent activity. A recipient-only user sees just the wallet. The top-bar KYC badge is shown only to users who can send (KYC gates sending). Progress bars: pink fill on a light-teal track with a teal hairline edge (a dark track read as "full" at low values); the text beside every bar carries the figures.

- **Top:** A large "Available to send today" figure, derived from the remaining daily limit, and a primary **Send money** button.
- **Limit tiles:** Daily and monthly limits as stat tiles with progress bars, showing used and remaining amounts in ZAR.
- **Activity:** Recent remittances, each with the recipient, the ZAR amount, the UCTUSD amount and a status badge. Include a "See all" link.

**Motion:**
- The "Available to send today" figure counts up on first load.
- The limit progress bars fill in.
- Show skeletons wherever data loads asynchronously.

### 7.3 Send flow and quote (pattern: Wise send flow) — ✅ 4b

*As built (4b, with review fixes):* the ZAR field is a text input with `inputmode="decimal"` (name/id unchanged), capped at **9 digits before the point and 2 after**: extra characters are rejected as you type, and `pattern`/`maxlength` enforce the same cap on the no-JS submit. There is no native spinner. When `/quote` returns `limit_ok: false` (or an error), the quote panel is **not** updated: the last valid quote stays, dimmed, with the warning under the amount; before any valid quote the skeletons stay. The warning's "over" figure is capped the same way. The shared breakdown never lets a label shrink below its natural width; a value that doesn't fit beside its label drops onto its own line (Review, Pay, Status and cash-out all use it). Stepper **Details → Review → Pay → Status** on `/send`, `/send/review`, `/send/pay` and `/transactions/{id}` (completed steps link back with the same `beneficiary_id`/`zar_amount`; the Status step is sender-only). `/send` keeps its GET form (works without JS) and adds a live breakdown fed only by `GET /quote` (300 ms debounce, stale responses dropped, `is-loading` dims, "Recipient gets" counts up, other rows cross-fade, skeletons before the first quote). Over a limit, an inline warning shows the server's `limit_reason` plus "That's R… over" — the only arithmetic on the page, a subtraction of two values `/quote` returns (`zar_amount` − `daily_remaining` or `monthly_remaining`). The Status page shows a Card payment → Settling on XRPL → Settled on XRPL timeline (`timeline()` macro) and carries `data-status-poll` for the 4c polling. History uses `transaction_badge`, `amount()` and `tx_hash()` (a hash shows whenever one exists — audit §4.8).

- **Stepper:** **Details → Review → Pay → Status**, matching the real routes: Details = `/send` (recipient and amount on one GET form; do **not** split it into separate routes), Review = `/send/review`, Pay = `/send/pay`, Status = `/transactions/{id}`. Do not change the logic.
- **Amount step:**
  - The ZAR input is prominent.
  - Below it, a live breakdown with one row each for: exchange rate, transaction fee, FX margin, net amount converted, **Recipient gets X UCTUSD** (the hero figure), cash-out fee, and estimated payout.
  - Every value comes from the existing `GET /quote` endpoint (JSON; also returns `limit_ok`, `limit_reason`, `daily_remaining`, `monthly_remaining`); do not recompute anything in the front end.
- **Limits:** If the amount would exceed the daily or monthly limit, show an inline error explaining which limit and by how much.
- **Review step:** A summary card and a clear confirm button.
- **Pay step:** The simulated card form (`/send/pay`; keep the hidden price-lock inputs). After submit, the **Status** step (`/transactions/{id}`) shows the reference and "Awaiting payment" until an admin confirms the cash-in, then the settlement progress.

**Motion:**
- **Stepper:** The connector fills between steps as the user advances.
- **Live quote:** As the ZAR amount changes, the figures update in place.
  - Debounce requests to `/quote` by ~300ms.
  - `countUp` moves "Recipient gets" to its new value; the other rows cross-fade.
  - While a request is in flight, dim the breakdown slightly. Do not blank it.
- **Limit error:** The inline error slides down in place. No shaking.
- **Confirmation:** On the Status page, when the cash-in is confirmed and the settlement validates (seen via polling), show a success state with `.ds-check-draw` and the summary card sliding up.

### 7.4 Recipient wallet (pattern: Revolut list and drawer)

- **Top:** The balance in large type (UCTUSD), with a **Cash out** button.
- **List:** Merge incoming remittances and **cash-outs (as outgoing)** into one list; the project brief requires the wallet to show outgoing/cash-out transactions. Each item shows an incoming/outgoing icon, a counterparty, a date, a signed amount and a status. Failed items are struck through with a red marker. `/cashout/history` stays, and gets a nav entry.
- **Detail drawer:** Clicking a transaction opens a right-hand drawer, or a full page on mobile. It shows:
  - status, amount, fee, exchange rate and dates;
  - the **XRPL transaction hash**, with a copy button and a link to the Testnet explorer.
- **Settlement states:** Queued and submitted transactions show a "Settling on XRPL…" state.
- **No-JS fallback:** Without JavaScript, the drawer can be a plain detail page.

**Motion.** This is the showcase moment of the demo.
- **In-progress settlements:** Queued and submitted transactions show `.ds-pulse-dot` next to "Settling on XRPL…".
- **Live status updates:** While any transaction on the page is in progress, poll its status every 2–3s.
  - Use a lightweight JSON status endpoint; one read-only GET route is allowed for this.
  - Stop polling once every transaction on the page has reached a final state, or after 2 minutes.
- **On validation:**
  - the badge colour-transitions to green;
  - `.ds-check-draw` plays once;
  - the balance counts up to its new value;
  - the XRPL hash appears in the row and drawer with a fade-in.
- **On failure:** The badge turns red and the amount strikes through, animating the line across. There is no celebratory motion.
- **Drawer:** It slides in from the right with the backdrop fade. The copy button shows "Copied" feedback.

### 7.5 Cash-out flow

**Layout:** Use the same breakdown pattern as the send flow:
- the UCTUSD amount;
- the rate;
- the cash-out fee;
- **You receive X USD** as the hero figure;
- a status timeline (requested → approved → completed).

**Motion:**
- The "You receive" figure uses the same `countUp` behaviour as the send flow.
- In the status timeline, the current step shows `.ds-pulse-dot` and completed steps are solid.

### 7.6 Admin area (pattern: Stripe dashboard)

- **Overview:** A new page at `/admin` (behind `require_admin`) and the admin landing page after login. Stat tiles for pending KYC, cash-ins awaiting confirmation, pending cash-outs, failed settlements and settlements validated today. *(Tiles shipped in Phase 3; Phase 4 adds the "Failed payments"-style list.)*
- **Queues:** KYC, cash-in and cash-out queues are clean tables. Each row has a status badge and inline Approve / Reject actions.
- **Actions:** Destructive or irreversible actions ask for confirmation. This includes cash-in received/failed, cash-out approve and settlement retry (in-page confirmation, not only native `confirm()`).
- **Failed settlements:** Listed prominently, like Stripe's "Failed payments".
- **Config pages:** Fee and limit configuration forms sit in cards with clear labels and units.

**Motion:**
- The stat tiles count up on load.
- An approved or rejected row updates its badge, then fades and collapses out of the pending queue. Use a JS-measured height or a `grid-template-rows` transition rather than animating `height` directly.
- Keep the admin area restrained; it is a work tool.

## 8. Phase 5: Secondary screens

These screens should be consistent with the design system but need less polish:
- landing page at `/` for signed-out users (new route; lower priority), including a simple hero inspired by the Uvodo and Stripe landing pages;
- login and register;
- profile at `/profile` (new route; lower priority);
- beneficiaries list and form;
- full transaction history;
- empty states (every list needs one);
- 404 and 403 pages (new HTML exception handlers; today FastAPI returns JSON).

## 9. Working rules for Claude Code

- **Approval gates:** Work phase by phase. Stop after Phase 1 and Phase 2 for review.
- **Dependencies:** Avoid heavy new ones. The project uses Bootstrap 5.3: **skin it** via CSS custom properties and in-place theming of its own classes. Never add a second framework or create classes that collide with Bootstrap's. New classes are prefixed `ds-`. Small vanilla JS is fine for the drawer, copy buttons and live quote updates.
- **Preserve behaviour:** Keep every form's `name`s, `action`s and methods unchanged (including the hidden price-lock inputs), keep existing element IDs, and keep the attribute order the perf harness scrapes (UI_AUDIT.md §6).
- **Tests:** Run the test suite after each screen. Keep any text the tests assert on verbatim (list in UI_AUDIT.md §6).
- **Security:** Never render private keys, seeds or secrets in any template, including dev pages.
- **Motion checks:** Test with reduced motion enabled. In Chrome DevTools, open Rendering and emulate `prefers-reduced-motion`. Every screen must still work and make sense with it on.
- **Verification:** After each phase, take screenshots at 1440px and 390px widths (headless browser if available). Compare them against this brief and the references, and fix what's off before reporting.
- **Reporting:** At the end of each phase, write a short summary covering what changed, what's left, and any decisions that need a human.