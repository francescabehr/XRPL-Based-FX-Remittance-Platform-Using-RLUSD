# UI Audit (Phase 1)

Read-only audit of `frontend/` against UI_REDESIGN.md. Nothing else changed.

## 1. Templates, routes and roles

28 templates, about 1,900 lines. All extend `base.html`. Every page route checks the session inline
(`get_current_user`) and redirects; admin routes use `require_admin`.

Roles are three booleans on `users`: `is_admin`, `can_send` (default true) and `can_receive`. There is
no separate recipient dashboard: every non-admin lands on `/dashboard`.

| Template | Route(s) | Seen by |
|---|---|---|
| `base.html` | layout for all pages | all |
| `_macros.html` | `hash_link(tx_hash)`, `send_steps(step)` | shared |
| `auth/login.html` | GET/POST `/login` | public |
| `auth/register.html` | GET/POST `/register` | public |
| — | GET `/` redirects to `/login` or `/dashboard` (**no landing page**) | public |
| — | GET `/logout` | all |
| `sender/dashboard.html` | GET `/dashboard` | every non-admin (sender and recipient) |
| `sender/kyc_form.html` | GET/POST `/kyc` | non-admin |
| `sender/beneficiaries.html` | GET `/beneficiaries`, POST `/beneficiaries/{id}/delete` | sender |
| `sender/beneficiary_form.html` | GET/POST `/beneficiaries/new`, `/beneficiaries/{id}/edit` | sender |
| `sender/send_amount.html` | GET `/send` (recipient and amount on one GET form) | approved sender |
| `sender/send_review.html` | GET `/send/review` | approved sender |
| `sender/send_pay.html` | GET `/send/pay`; re-rendered by POST `/remittances` on card error | approved sender |
| `sender/_quote_table.html` | included by review and pay | approved sender |
| `sender/transactions.html` | GET `/transactions` (admins are redirected to `/admin/cashin`) | sender |
| `sender/transaction_detail.html` | GET `/transactions/{id}`, also the post-pay landing page | **sender, recipient and admin** (despite its folder) |
| `recipient/wallet.html` | GET `/wallet` | non-admin (linked in nav only if `can_receive`) |
| `recipient/cashout_form.html` | GET `/cashout`; POST `/cashout/preview` on error | `can_receive` |
| `recipient/cashout_review.html` | POST `/cashout/preview` | `can_receive` |
| `recipient/cashout_history.html` | GET `/cashout/history` (POST `/cashout` redirects here) | `can_receive` |
| `recipient/cashout_detail.html` | GET `/cashout/{id}` | `can_receive` |
| `admin/kyc_queue.html` | GET `/admin/kyc` (admin login lands here) | admin |
| `admin/kyc_detail.html` | GET `/admin/kyc/{id}`, POST `/approve`, `/reject` | admin |
| `admin/cashin_queue.html` | GET `/admin/cashin`, POST `/{id}/received`, `/{id}/failed` | admin |
| `admin/settlements.html` | GET `/admin/settlements`, POST `/{id}/retry` | admin |
| `admin/cashout_queue.html` | GET `/admin/cashout`, POST `/approve`, `/reject`, `/reconcile` | admin |
| `admin/transactions.html` | GET `/admin/transactions` (filters) | admin |
| `admin/transaction_detail.html` | GET `/admin/transactions/{id}`, POST `/{id}/aml` | admin |
| `admin/config.html` | GET `/admin/config`, POST `/config/tiers/{id}`, `/config/fees` | admin |

JSON-only: GET `/quote` (needs an approved sender; returns every quote field plus `limit_ok`,
`limit_reason`, `daily_remaining` and `monthly_remaining`) and PATCH `/transactions/{id}/cashin` (admin).

**Screens the brief expects that don't exist yet:** a landing page, a profile page, 404/403 pages
(`require_admin` raises a 403 that FastAPI renders as JSON `{"detail": ...}`, and unknown URLs get
JSON 404s), an admin overview, `/dev/styleguide`, and a JSON settlement-status endpoint for polling.

## 2. Styling approach

- **Bootstrap 5.3.3 and Bootstrap Icons 1.11.3** load from the jsDelivr CDN in `base.html`, with
  `bootstrap.bundle.min.js` at the end of the body.
- `frontend/static/css/style.css` (23 lines) holds small tweaks: card radius, progress radius and
  `dl` weight. It is served at `/static/css/style.css` (`app/main.py` mounts `frontend/static`).
- Nearly all styling uses Bootstrap utility classes inline in the markup (`fw-bold`, `text-muted`,
  `shadow-sm`, `d-flex`…). There are a few inline `style=` attributes: `max-width: 560px` on send
  cards, progress-bar `width:%` and `height:10px`, and input-group `max-width:180px` in config.
- There are no design tokens, no custom font (the system stack), a dark navbar, a `bg-light` body and
  Bootstrap's default blue primary.
- **Bootstrap JS in use:** navbar collapse, the user dropdown, `.alert` dismiss, and `collapse` rows
  for the inline "Mark failed" (cash-in) and "Reject" (cash-out) forms.
- There is **no project JavaScript** at all. `beneficiaries` delete and `kyc_detail` approve/reject use
  inline `onsubmit="return confirm(...)"`.
- Jinja env (`app/templating.py`): filter `localtime`, global `explorer_tx_url`. `settings.debug`
  (default False) exists and can gate `/dev/styleguide`.

## 3. Repeated UI pieces → shared components

| Pattern | Where it repeats today | Proposed component |
|---|---|---|
| Status badge `<span class="badge bg-{{ x.status_badge }}">{{ x.status_label }}</span>` | dashboard, transactions, transaction_detail, wallet, cashout_history/detail, cashout_queue, admin transactions/detail, settlements | `status_badge(status, domain)`: one map (see §5) |
| Ad-hoc inline badge maps (`{'received':'success',…}.get(...)`, `'danger' if … else 'warning text-dark'`) | admin/transactions, admin/transaction_detail, settlements | same macro. These maps currently disagree with the model properties |
| Money formatting `R{{ "{:,.2f}".format(x) }}`, `{{ "{:,.6f}".format(x) }} UCTUSD`, `$…` / `R…` switch on `target_currency` | about 40 occurrences in 15 templates | `amount(value, currency, size)` |
| Label/value breakdown table (`<tr><th class="fw-normal text-muted">…</th><td class="text-end">…`) | _quote_table, transaction_detail, cashout_review, cashout_detail, admin transaction_detail | `breakdown_row(label, value, emphasis)` + `.breakdown` |
| Card with header (`card shadow-sm` + `card-header fw-semibold`) | almost every page | `.card` restyle |
| Page header (h3 + icon + right-aligned action or count badge) | about 15 pages | `page_header(title, action)` or the top-bar title from the Phase 3 shell |
| Back button (`btn-sm btn-outline-secondary` + arrow) | beneficiary_form, transaction_detail, kyc_detail, admin transaction_detail, cashout_detail (text link) | part of `page_header` |
| Empty state (big icon + bold line + optional CTA), in 3 different markups | beneficiaries, transactions, wallet, cashout_form/history, all admin queues | `empty_state(title, message, action)` |
| Flash / inline alert (`alert alert-{{ kind }}`), where `kind` ∈ success/danger/warning/info | base.html flash + `error`/`card_error` alerts on 8 forms | `.flash` variants |
| Hash link | `_macros.hash_link` (already shared) | keep, add copy button |
| Stepper | `_macros.send_steps` (3 steps) | `stepper(steps, current)` |
| Limit progress bar with 70/90% colour thresholds | dashboard (twice) | `stat_tile` + progress |
| Form field (`form-label fw-semibold` + control + `form-text`) | all forms | CSS only. Labels are not tied to inputs by `for`/`id` on login, register, kyc, beneficiary_form, config |
| Admin queue table + inline action forms | kyc_queue, cashin_queue, cashout_queue, settlements | `.table` restyle + row-action pattern |

## 4. Navigation, dead links and inconsistencies

**Dead links: none found.** Every `href` and `action` in the templates resolves to a route. The brief's
examples `/admin/cashout` and `/admin/transactions` are live, `require_admin`-guarded routes (Phase 7/8).

Inconsistencies:
1. **No active-page state** in the nav.
2. **Nav ignores roles:** Send Money, Beneficiaries and History show to every non-admin even when
   `can_send` is false. Wallet and Cash Out correctly depend on `can_receive`.
3. **`/cashout/history` has no nav entry.** You can only reach it from the cash-out form, the wallet
   footnote and the post-request redirect.
4. **Recipients land on the sender dashboard** (KYC banner, limits, "Send Money"). The dashboard has
   no wallet balance.
5. **Admin nav order** doesn't follow the workflow (KYC → Transactions → Cash-In → Settlements →
   Cash-Out → Config). "Settlements" uses a warning-triangle icon.
6. **Status wording differs by screen for the same state:** the sender history says "Queued/Sending",
   admin monitor badges say "Processing", `transaction_detail` capitalises raw enum values
   ("Received", "Not queued"), and settlements only show "Failed"/"Queued"/"Processing".
7. **Colour disagreements:** in-flight transfers are `info` (blue) on user screens and `secondary`
   (grey) on the admin monitor. Cash-out `requested` is grey, but the brief says amber.
8. **Hash visibility differs:** sender history hides the hash unless completed; wallet and detail
   always show it.
9. **The "Send Money" disabled state** is a `.disabled` class on an `<a>`, so it is still reachable
   by keyboard (the server redirects with a flash, so it's safe but clumsy).
10. **Confirmation is inconsistent:** beneficiary delete and KYC approve/reject use native
    `confirm()`, but cash-in "Received"/"Failed", cash-out "Approve" (it debits the balance) and
    settlement "Retry" have none.
11. **The transaction_detail back link** is computed per role (`/transactions`, `/wallet`,
    `/admin/settlements`). An admin opening it from the monitor goes back to settlements.
12. **Branding:** "XRPL Remit" in the navbar and titles, and "XRPL Remittance" as the default title.
13. **Accessibility gaps:** the navbar toggler has no `aria-label`, icons lack `aria-hidden`, labels
    lack `for` (see §3), and limit bars use colour alone. Status badges always have text, which is good.

## 5. Status vocabulary: code vs brief §6

The brief's table doesn't match the models. **Proposed replacement for §6** (follow the code):

| Domain | Enum values (code) | Proposed label → colour |
|---|---|---|
| KYC (`users.kyc_status`) | not_submitted, pending, approved, rejected | Not started → grey; Awaiting approval → amber; Verified → green; Rejected → red |
| KYC submission (`kyc_submissions.status`) | pending, approved, rejected | as above |
| Cash-in (`transactions.cashin_status`) | **pending, received, failed** (brief: awaiting_payment/confirmed) | Awaiting payment → amber; Payment received → green; Failed → red |
| Settlement (`transactions.settlement_status`) | **not_queued, queued, processing, completed, failed** (brief: queued/submitted/validated) | Not started → grey; Queued → amber; Settling on XRPL… → amber + pulse; Settled on XRPL → green; Failed → red |
| Transaction overall (`Transaction.status_label`) | derived: Awaiting payment, Payment received, Queued, Sending, Completed, Failed | reuse the cash-in/settlement rows |
| Cash-out (`cashout_requests.status`) | requested, approved, completed, failed + derived **"Awaiting ledger confirmation"** (approved + hash + `outcome_unknown`) | Requested → amber; Approved/burning → amber + pulse; Awaiting ledger confirmation → amber; Completed → green; Failed → red |
| Beneficiary link | Linked / Not registered (not an enum) | green / grey |
| AML flag | boolean | red "AML review" |

Keep `status_label`/`status_badge` on the models untouched; the macro maps enum values itself. **Tests
assert on "Awaiting ledger confirmation", so that text must stay verbatim.**

## 6. Constraints to preserve in Phases 2–5

- **Form contracts:** every `name`, `action` and `method`, including the hidden price-lock inputs on
  `send_pay` (`exchange_rate`, `transaction_fee`, `uctusd_amount`) and on `cashout_review`
  (`market_rate`, `cashout_fee_usd`, `net_payout`). There are no CSRF tokens in this app, despite the
  brief's mention.
- **IDs:** `#beneficiary_id`, `#zar_amount`, `#card_name/#card_number/#card_expiry/#card_cvv`,
  `#uctusd_amount`, `#target_currency`, and the collapse targets `#fail-{id}` and `#reject{n}`.
- **Text that tests assert on:** "Continue to payment", "50.874404 UCTUSD" (six decimals, a space, then
  the code), "R1,000.00", "•••• 4242", "Available", "Cash-out queue", "Awaiting ledger confirmation",
  "simulated", "Earlier attempts", "Ledger balance unavailable", "tesSUCCESS", the full explorer URL
  `https://testnet.xrpl.org/transactions/<hash>`, the idempotency key, "/admin/config/fees",
  "/admin/config/tiers/", and the auth errors "do not match" and "Invalid".
- **Perf harness regexes** (`perf/locustfile.py`): `<option value="<uuid>"` on `/send`,
  `name="exchange_rate" value="…"` attribute order on `/send/pay`, `name="market_rate" value=…` →
  `cashout_fee_usd` → `net_payout` order on the preview, and `/admin/cashin/<uuid>/received` in the queue.
- **Any new `/admin*` route must depend on `require_admin`.** `test_every_admin_route_uses_require_admin`
  walks `app.routes`, and non-admins must get 403 on all of them.
- Never render seeds or keys (none are in template context today; keep it that way on `/dev/styleguide`).

## 7. Proposed screen split

**Phase 4, demo path (in the brief's order):**
1. Onboarding + KYC: "Get started" checklist on `sender/dashboard.html`; sectioned `sender/kyc_form.html`.
2. Sender dashboard: `sender/dashboard.html` (available today, limit tiles, recent activity).
3. Send flow: `send_amount` → `send_review` → `send_pay` → `transaction_detail` (the post-pay status
   page). The stepper shows Recipient → Amount → Review → Pay, but **Recipient and Amount share `/send`**
   (one GET form); the live quote calls the existing `GET /quote`.
4. Recipient wallet: `recipient/wallet.html` + detail drawer (server-rendered per row;
   `/transactions/{id}` is the no-JS fallback) + polling via one new read-only JSON status route.
5. Cash-out: `cashout_form` → `cashout_review` → `cashout_detail` (timeline).
6. Admin: **new** overview + `kyc_queue`, `kyc_detail`, `cashin_queue`, `cashout_queue`, `settlements`, `config`.

**Phase 5, secondary:** landing page (new), `auth/login`, `auth/register`, profile (new),
`beneficiaries` + `beneficiary_form`, `sender/transactions` (full history), `cashout_history`,
`admin/transactions` + `admin/transaction_detail`, and 404/403 pages (new exception handlers).
Empty states are applied everywhere.

## 8. Brief vs code, and other notes

- **Paths:** static is `frontend/static/` (served at `/static/`), so the new files will be
  `frontend/static/css/design-system.css`, `frontend/static/js/ui.js` and `frontend/templates/components/`.
  The existing `_macros.html` (`hash_link`, `send_steps`) should move into `components/`.
- The **status table** (§5 above) and the **dead-link examples** are both out of date.
- **Pay step:** the brief describes "cash-in instructions, a reference number, awaiting confirmation".
  The real pay step is a simulated card form. The reference and "Awaiting payment" status appear on
  `/transactions/{id}` after submit, so that page plays the brief's confirmation role. After submit,
  cash-in stays pending until an admin confirms it, so the brief's "cash-in confirmed" success moment
  can only happen on the status page (via polling).
- **Wallet "incoming and outgoing":** the wallet lists incoming remittances only; cash-outs (the only
  outgoing movement) live on `/cashout/history`.
- **Admin row fade-out:** admin actions are form POST → redirect → full reload, so the row just
  disappears. Animating it needs JS `fetch` interception or a flash-driven "just-removed" row.
- **Unlisted reference:** `design_references/base-processing.png` (a centred "Processing / Do not close
  this window" card) isn't in the brief's table. It suits the "Settling on XRPL…" state.
- **Existing framework:** the brief says to work with an existing framework. Its class names `.btn`,
  `.card`, `.badge` and `.flash` collide with Bootstrap's `.btn`, `.card` and `.badge`.


## 9. Open questions before Phase 2
1. **Bootstrap:** skin it (recommended: map tokens onto Bootstrap CSS variables, override
   `.btn/.card/.badge`, keep grid/collapse/dropdown) or remove it and hand-write everything?
2. **New routes:** OK to add `/dev/styleguide` (debug only), one JSON status GET, an admin overview
   (`/admin`, `require_admin`), 404/403 HTML handlers, a public landing page at `/` for signed-out
   users, and `/profile`? Should admin login land on the overview instead of `/admin/kyc`?
3. **Wallet outgoing:** merge cash-outs into the wallet list (the router passes one extra list) or
   keep incoming only?
4. **Recipient home:** keep one shared `/dashboard`, or add a wallet-balance section for `can_receive` users?
5. **Status labels:** approve the §5 table as the replacement for brief §6.


## 10. Decisions (Phase 1 review)

1. **Bootstrap:** skin it, don't remove it. Tokens map onto Bootstrap's CSS variables and its own `.btn/.card/.badge/.alert` are themed in place; grid, collapse and dropdown JS stay. New classes only for things Bootstrap lacks, prefixed `ds-`.
2. **New routes approved:** `/dev/styleguide` (debug only), one read-only JSON status GET for polling, an admin overview at `/admin` (behind `require_admin`, and the admin landing page after login), HTML 404/403 handlers, a landing page at `/` for signed-out users, and `/profile`. Landing and profile are Phase 5, lower priority.
3. **Wallet:** merge cash-outs into the wallet list as outgoing transactions (the project brief requires it). Keep `/cashout/history` and add it to the nav.
4. **Recipient home:** one shared `/dashboard`, role-aware: a wallet-balance section with Cash out for `can_receive`; send/KYC/limits content only for `can_send`.
5. **Status labels:** the §5 table is approved and now replaces UI_REDESIGN.md §6. Test-asserted text stays verbatim.
6. **Send flow:** `/send` stays one route. The stepper is Details (recipient + amount) → Review → Pay → Status (`/transactions/{id}`). The live quote calls the existing `GET /quote`.
7. **Amounts:** `R1,000.00`, `$1,000.00`, `50.874404 UCTUSD`, with the symbol/code and number in one text node.
8. **Phase 3** fixes the §4 navigation and accessibility issues. Irreversible admin actions (cash-in received/failed, cash-out approve, settlement retry) get confirmation.
