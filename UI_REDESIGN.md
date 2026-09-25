# UI Redesign Brief

This brief is for Claude Code. It explains how to redesign the front end of our XRPL remittance platform. Read the whole brief before changing anything, then follow the phases in order.

## 0. Context

- **App:** ECO5040W class project, a cross-border remittance prototype. A sender in South Africa pays ZAR, and the recipient receives UCTUSD, a test IOU on the XRPL Testnet, in a custodial web wallet. The recipient can then request a simulated fiat cash-out.
- **Stack:** Python back end with server-rendered Jinja templates under `frontend/templates/`, extending `base.html`, and PostgreSQL.
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

**Rules for using the references:**
- Borrow **patterns and layout ideas only**.
- Never copy logos, product names, illustrations, wordmarks or exact brand colours.
- Our product must look like its own brand.

## 2. Design direction

The look should be calm, confident and precise: a trustworthy money app, not a crypto casino.

- **Background:** Near-white app background, with white cards on top separated by very subtle borders or shadows.
- **Accent:** One accent colour, a deep indigo/violet, used sparingly for primary actions, active navigation and key figures.
  - Semantic colours are reserved for status and carry meaning, not decoration: green for success, amber for pending, red for failed, blue/grey for info.
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
4. **Navigation:** Flag dead links (e.g. `/admin/cashout`, `/admin/transactions`) and inconsistencies.
5. **Plan:** Propose which screens fall under the demo path (Phase 4) and which are secondary (Phase 5).

Write the audit to `UI_AUDIT.md` in the project root, then stop and wait for approval.

## 4. Phase 2: Design system foundation

Build `static/css/design-system.css`, or the project's equivalent static path. It should define:

- **CSS custom properties on `:root`:**
  - colour tokens: `--bg`, `--surface`, `--border`, `--text`, `--text-muted`, `--accent`, `--accent-hover`, `--accent-soft`, `--success`, `--warning`, `--danger`, `--info`, each with a `-soft` background variant;
  - a type scale, including `--text-amount-xl` for hero amounts;
  - a spacing scale on a 4px base;
  - radius values;
  - one or two shadows.
- **Base element styles:** body, headings, links, form inputs, selects, labels, focus rings (visible and accessible), and tables.
- **Component classes:** `.btn` (primary / secondary / ghost / danger), `.card`, `.badge` with a status modifier, `.amount`, `.stat-tile`, `.stepper`, `.drawer`, `.empty-state`, `.flash`.

Also create Jinja macros in `templates/components/`:
- `status_badge(status)`: a single source of truth that maps each status to a colour and label (see section 6).
- `amount(value, currency, size)`
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

**Utilities to build in `design-system.css`:**
- `.fade-in` and `.slide-up`: small entrance animations, 8px of travel.
- `.skeleton`: a grey block with a shimmer sweep, used as a loading placeholder.
- `.pulse-dot`: a small pulsing dot for in-progress states.
- `.check-draw`: an SVG check mark that draws itself with `stroke-dashoffset`.
- `.drawer` open and close: slides in from the right, with a backdrop fading to ~40% opacity.
- `.flash` enter and auto-dismiss:
  - slides in from the top;
  - auto-dismisses after 5s for success and info messages only;
  - errors stay until closed.
- Button and card hover and press:
  - buttons: darken on hover and scale to 0.98 on press;
  - cards: a slight lift on hover (`translateY(-2px)` plus a stronger shadow), applied only to clickable cards.

**JS helpers.** Add a small `static/js/ui.js`, loaded with `defer`:
- `countUp(el, to, {duration})`: animates a number to a new value with tabular numerals and correct decimal places. It respects reduced motion.
- `copyToClipboard(btn)`: swaps the label to "Copied" for 1.5s.
- Drawer open and close:
  - traps focus inside the drawer and closes on Esc;
  - returns focus to the element that opened it.
- A polling helper for settlement status (see 7.4).

Add a **Motion** section to `/dev/styleguide` that demos every utility. It needs a toggle that simulates reduced motion.

## 5. Phase 3: App shell

Rebuild `base.html`:
- **Sidebar:**
  - the logo or wordmark (a simple text wordmark is fine);
  - role-aware navigation, meaning sender, recipient and admin links shown only to those roles;
  - the active state highlighted with `--accent-soft`;
  - user name and initials avatar at the bottom, with logout.
- **Top bar:** page title, plus the KYC status badge for non-admin users.
- **Flash messages:** styled with the `.flash` variants.
- **Dead links:** remove or hide nav links that have no route.

## 6. Status vocabulary

Every status is displayed through the `status_badge` macro.

| Domain | Statuses |
|---|---|
| KYC | not_submitted, pending, approved, rejected |
| Cash-in | awaiting_payment, confirmed, failed |
| Settlement | queued, submitted, validated, failed |
| Cash-out | requested, approved, completed, failed |

- **Colours:** green for approved, confirmed, validated and completed; amber for pending, awaiting_payment, queued, submitted and requested; red for rejected and failed; grey for not_submitted.
- **Match the code:** Use the real status values from the models. If they differ from this table, follow the code and update this table.

## 7. Phase 4: Demo-path screens (highest priority)

Redesign these screens in order. After each one, run the tests and check it at desktop and phone widths.

### 7.1 Onboarding and KYC (pattern: Uvodo checklist)

**Placement:** On the sender dashboard, until the user is fully set up.

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
- A step that has just been completed shows `.check-draw` once.

### 7.2 Sender dashboard (pattern: Wise home)

- **Top:** A large "Available to send today" figure, derived from the remaining daily limit, and a primary **Send money** button.
- **Limit tiles:** Daily and monthly limits as stat tiles with progress bars, showing used and remaining amounts in ZAR.
- **Activity:** Recent remittances, each with the recipient, the ZAR amount, the UCTUSD amount and a status badge. Include a "See all" link.

**Motion:**
- The "Available to send today" figure counts up on first load.
- The limit progress bars fill in.
- Show skeletons wherever data loads asynchronously.

### 7.3 Send flow and quote (pattern: Wise send flow)

- **Stepper:** Recipient → Amount → Review → Pay (cash-in). Map these steps onto the existing routes and forms; do not change the logic.
- **Amount step:**
  - The ZAR input is prominent.
  - Below it, a live breakdown with one row each for: exchange rate, transaction fee, FX margin, net amount converted, **Recipient gets X UCTUSD** (the hero figure), cash-out fee, and estimated payout.
  - Every value comes from the existing `/quote` endpoint; do not recompute anything in the front end.
- **Limits:** If the amount would exceed the daily or monthly limit, show an inline error explaining which limit and by how much.
- **Review step:** A summary card and a clear confirm button.
- **Pay step:** The simulated cash-in instructions, e.g. a reference number, and a status of "awaiting confirmation".

**Motion:**
- **Stepper:** The connector fills between steps as the user advances.
- **Live quote:** As the ZAR amount changes, the figures update in place.
  - Debounce requests to `/quote` by ~300ms.
  - `countUp` moves "Recipient gets" to its new value; the other rows cross-fade.
  - While a request is in flight, dim the breakdown slightly. Do not blank it.
- **Limit error:** The inline error slides down in place. No shaking.
- **Confirmation:** When cash-in is confirmed, show a success state with `.check-draw` and the summary card sliding up.

### 7.4 Recipient wallet (pattern: Revolut list and drawer)

- **Top:** The balance in large type (UCTUSD), with a **Cash out** button.
- **List:** Transactions show incoming and outgoing icons, a counterparty, a date, a signed amount and a status. Failed items are struck through with a red marker.
- **Detail drawer:** Clicking a transaction opens a right-hand drawer, or a full page on mobile. It shows:
  - status, amount, fee, exchange rate and dates;
  - the **XRPL transaction hash**, with a copy button and a link to the Testnet explorer.
- **Settlement states:** Queued and submitted transactions show a "Settling on XRPL…" state.
- **No-JS fallback:** Without JavaScript, the drawer can be a plain detail page.

**Motion.** This is the showcase moment of the demo.
- **In-progress settlements:** Queued and submitted transactions show `.pulse-dot` next to "Settling on XRPL…".
- **Live status updates:** While any transaction on the page is in progress, poll its status every 2–3s.
  - Use a lightweight JSON status endpoint; one read-only GET route is allowed for this.
  - Stop polling once every transaction on the page has reached a final state, or after 2 minutes.
- **On validation:**
  - the badge colour-transitions to green;
  - `.check-draw` plays once;
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
- In the status timeline, the current step shows `.pulse-dot` and completed steps are solid.

### 7.6 Admin area (pattern: Stripe dashboard)

- **Overview:** Stat tiles for pending KYC, cash-ins awaiting confirmation, pending cash-outs, failed settlements and settlements validated today.
- **Queues:** KYC, cash-in and cash-out queues are clean tables. Each row has a status badge and inline Approve / Reject actions.
- **Actions:** Destructive or irreversible actions ask for confirmation.
- **Failed settlements:** Listed prominently, like Stripe's "Failed payments".
- **Config pages:** Fee and limit configuration forms sit in cards with clear labels and units.

**Motion:**
- The stat tiles count up on load.
- An approved or rejected row updates its badge, then fades and collapses out of the pending queue. Use a JS-measured height or a `grid-template-rows` transition rather than animating `height` directly.
- Keep the admin area restrained; it is a work tool.

## 8. Phase 5: Secondary screens

These screens should be consistent with the design system but need less polish:
- landing page, including a simple hero inspired by the Uvodo and Stripe landing pages;
- login and register;
- profile;
- beneficiaries list and form;
- full transaction history;
- empty states (every list needs one);
- 404 and 403 pages.

## 9. Working rules for Claude Code

- **Approval gates:** Work phase by phase. Stop after Phase 1 and Phase 2 for review.
- **Dependencies:** Avoid heavy new ones. Plain CSS with custom properties is preferred; if the project already uses a CSS framework, work with it rather than adding another. Small vanilla JS is fine for the drawer, copy buttons and live quote updates.
- **Preserve behaviour:** Keep every form's `name`s, `action`s and CSRF tokens unchanged, and keep existing element IDs that tests depend on.
- **Tests:** Run the test suite after each screen. If tests assert on HTML text that you have changed, update the assertions only where the change is purely cosmetic, and list those changes.
- **Security:** Never render private keys, seeds or secrets in any template, including dev pages.
- **Motion checks:** Test with reduced motion enabled. In Chrome DevTools, open Rendering and emulate `prefers-reduced-motion`. Every screen must still work and make sense with it on.
- **Verification:** After each phase, take screenshots at 1440px and 390px widths (headless browser if available). Compare them against this brief and the references, and fix what's off before reporting.
- **Reporting:** At the end of each phase, write a short summary covering what changed, what's left, and any decisions that need a human.