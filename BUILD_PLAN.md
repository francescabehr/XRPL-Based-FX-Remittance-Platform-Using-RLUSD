# BUILD_PLAN.md — Detailed Phase Guide (UCTUSD)

Companion to `CLAUDE.md`. Expands the remaining phases (4–9) into ordered, checkable steps.
Phases 0–3 are done. **Do one phase at a time**; do not start a phase until the one above it has
passing tests. Each phase ends with a **Done when** gate — treat it as the definition of complete.

Conventions used below:
- **Money is `Decimal`, never `float`.** ZAR to 2 dp, UCTUSD to 6 dp, rates to 6 dp.
- Every task lists the FR IDs it satisfies so nothing is orphaned.
- "Test" rows are the minimum unit tests; add more if a branch is untested.

---

## Phase 4 — FX Quote Engine  (FR-FX-01..08, un-stubs FR-LIM)

**Objective:** a `GET /quote` endpoint that turns a ZAR send amount into a full, transparent quote,
backed by a new `Transaction` model. This is the keystone — it introduces the model that un-stubs
limit tracking and feeds cash-in.

**Depends on:** reconciliation pass merged (config + UCTUSD naming in place).

### Steps
1. **Rate source.** Add `services/fx_service.py` with `get_market_rate() -> Decimal` returning
   USD/ZAR (ZAR per 1 USD). Start with a configurable mock/table value from `fee_config` or a
   `RATE_SOURCE` env var; wrap it so a live API can be swapped in later without changing callers.
   *(FR-FX-01)*
2. **Fee config.** Confirm `fee_config` has: `fixed_fee_zar`, `percentage_fee`, `fx_margin`,
   `cashout_fee_percentage` (and a `cashout_fee_min_usd` if you want a floor). Seed one active row
   with starting defaults (suggested: fixed R25, 1.5%, 2% margin, 1% cash-out, min $1 — **confirm
   your team's numbers**). *(FR-FX-08)*
3. **Calculation, in this exact order** (put it in `fx_service.calculate_quote`):
   - `transaction_fee = fixed_fee_zar + (percentage_fee * zar_send)`  *(FR-FX-02)*
   - `effective_rate  = market_rate * (1 + fx_margin)`  *(FR-FX-03)*
   - `net_zar         = zar_send - transaction_fee`  *(FR-FX-04)*
   - `uctusd_amount   = net_zar / effective_rate`  *(FR-FX-05)*
   - `cashout_fee_est = max(cashout_fee_percentage * uctusd_amount, min_usd)`  *(FR-FX-06)*
   - `payout_est`     = for USD: `uctusd_amount - cashout_fee_est`; for ZAR:
     `(uctusd_amount - cashout_fee_est) * market_rate`  *(FR-FX-06)*
   - **Worked example** (sanity check your rounding): zar_send=1000, fixed=25, pct=1.5%,
     margin=2%, market=18.50 → fee=40, net=960, eff=18.87, **uctusd≈50.874404**.
4. **Transaction model** (`models/transaction.py`) per CLAUDE.md, with `uctusd_amount`,
   `exchange_rate`, `fx_margin`, `transaction_fee`, `net_zar_converted`, `idempotency_key`
   (UUID UNIQUE), `cashin_status` default `pending`, `settlement_status` default `not_queued`.
   Generate the Alembic migration.
5. **Schemas** (`schemas/transaction.py`): `QuoteRequest{ beneficiary_id, zar_amount }` and
   `QuoteResponse` exposing all seven display figures. *(FR-FX-07)*
6. **Router:** `GET /quote` (query or JSON body) → 200 with `QuoteResponse`. Requires an
   authenticated, KYC-approved sender. Reject non-approved with a clear 403 *(ties FR-KYC-04)*.
7. **Un-stub limits.** Replace the hardcoded `Decimal("0")` in `limit_service.get_daily_usage` /
   `get_monthly_usage` with real sums over `transactions` for the user in the current day/month.
   Run the limit check **inside the same DB transaction** as any future transaction insert (note for
   Phase 6). *(FR-LIM-01..04)*
8. **UI:** Send Money Step 1 (amount + beneficiary) and Step 2 (quote review) Jinja templates
   showing all seven figures.

### Acceptance
- Quote shows ZAR amount, exchange rate, transaction fee, FX margin, UCTUSD amount, cash-out fee,
  estimated payout — all seven on one screen. *(FR-FX-07)*
- Changing a `fee_config` value changes new quotes, not past ones. *(FR-FX-08)*
- An over-limit amount is rejected before any payment step, with the remaining allowance shown.
  *(FR-LIM-03)*

### Tests (`test_fx.py`, extend `test_limits.py`)
- Fee/margin/UCTUSD math matches the worked example to 6 dp.
- Rounding is `Decimal`, not float (assert exact values).
- Daily/monthly usage now reflects inserted transactions, not 0.

**Done when:** `GET /quote` returns a correct full quote, limits compute from real data, all tests green.

---

## Phase 5 — XRPL Standalone De-Risk  (FR-WAL-01..04)

**Objective:** prove the entire on-chain path in a throwaway script **before** it touches the app or
worker. If anything about UCTUSD is going to bite (currency code, trust line, reserves, rippling),
find it here.

**Depends on:** verified `XRPL_CURRENCY_CODE`, `XRPL_ISSUER_ADDRESS`, `XRPL_PLATFORM_WALLET_SEED`.

### Steps (write as `scripts/xrpl_smoke.py`, run against Testnet)
1. **Client + treasury.** `JsonRpcClient(XRPL_JSON_RPC)`; load treasury via
   `Wallet.from_seed(XRPL_PLATFORM_WALLET_SEED)`; assert its classic address == `rMcBddj7…`.
2. **Create a recipient account.** `generate_faucet_wallet(client, debug=True)` — funds it with test
   XRP. Print address + seed. *(FR-WAL-01)*
3. **Reserve awareness.** Note the account base reserve plus **2 XRP locked per trust line**. The
   faucet balance covers this on Testnet, but assert the recipient has enough before TrustSet.
4. **TrustSet** recipient → issuer using the **exact** currency code:
   ```python
   TrustSet(account=recip.address,
            limit_amount=IssuedCurrencyAmount(currency=CURRENCY, issuer=ISSUER, value="1000000"))
   ```
   Submit with `submit_and_wait`; assert `meta.TransactionResult == "tesSUCCESS"`. *(FR-WAL-02)*
5. **Fund from treasury.** `Payment(account=treasury.address, destination=recip.address,`
   `amount=IssuedCurrencyAmount(currency=CURRENCY, issuer=ISSUER, value="10"))`; submit; assert
   success; capture the **tx hash**.
6. **Validate on-ledger.** Re-query the recipient's `account_lines`; assert the UCTUSD balance is 10
   and the counterparty is the issuer. Open the hash on `test.bithomp.com` to eyeball it.
7. **Burn round-trip** (proves Phase 7's mechanism): `Payment(account=recip.address,`
   `destination=ISSUER, amount=IssuedCurrencyAmount(currency=CURRENCY, issuer=ISSUER, value="5"))`;
   assert success; re-check balance is 5.
8. **Encryption round-trip.** Fernet-encrypt the recipient seed, decrypt, assert equality — this is
   exactly what `security/crypto.py` will do in-app. *(FR-WAL-03)*
9. **Failure taxonomy — trigger and record each:** payment to an account with **no trust line**
   (expect `tecPATH_DUST`/`tecNO_LINE`), payment exceeding balance (expect `tecPATH_PARTIAL`/
   `tecUNFUNDED_PAYMENT`), a malformed currency code. Note the exact result codes — the worker will
   branch on these.

### Acceptance
- A recipient account is created, trust-lined, funded, validated, and burned end-to-end, with every
  step printing a `tesSUCCESS` hash you can open in the explorer.
- Rippling confirmed working (the treasury→recipient payment finds a path because the issuer has
  Default Ripple). Note this for the spec; you do **not** configure it.

**Done when:** the script runs clean start-to-finish, and you've recorded the failure result codes.
Then port the proven calls into `services/xrpl_service.py` (async client) — **do not** re-derive them.

---

## Phase 6 — Cash-In + Queue + Settlement  (FR-CI-01..05, FR-MQ-01..06, FR-WAL-05..07)

**Objective:** confirmed cash-in → queued message → worker → treasury→recipient UCTUSD payment →
validated → balances updated, with strict idempotency.

**Depends on:** Phase 4 (Transaction model) and Phase 5 (proven XRPL calls).

### Steps
1. **Create transaction from quote.** `POST /remittances` takes a confirmed quote, inserts a
   `transactions` row with a fresh `idempotency_key`, runs the limit check **in the same DB
   transaction** as the insert. `cashin_status=pending`, `settlement_status=not_queued`. *(FR-LIM,
   FR-CI-01)*
2. **Simulated card.** Send Money Step 3 captures mock card fields (test values only); no real
   processing. *(FR-CI-01)*
3. **Confirm / fail cash-in.** A mock payment service or admin action PATCHes `cashin_status` to
   `received` or `failed`, timestamped. *(FR-CI-02, FR-CI-05)*
4. **Gate settlement.** No settlement message is enqueued while cash-in is `pending`/`failed`; a
   `failed` cash-in sets the transaction to failed and attempts no transfer. *(FR-CI-03, FR-CI-04)*
5. **Enqueue on received.** On `received`, `queue_service.enqueue_settlement(txn_id)` publishes to
   Redis/RQ carrying the `idempotency_key`. *(FR-MQ-01, FR-MQ-03)*
6. **Wallet provisioning (worker-side).** Worker ensures the recipient has a wallet row + on-ledger
   account + `trust_set_complete=True`; if not, provision via `xrpl_service` (create account,
   TrustSet, encrypt seed). *(FR-WAL-01..03)*
7. **Idempotent settlement.** Worker's **first** action is to claim the `idempotency_key`
   (conditional update / status check). If already terminal, ack and stop — no second credit.
   *(FR-MQ-04)*
8. **Submit + validate.** Submit treasury→recipient UCTUSD `Payment`; on `tesSUCCESS` store
   `xrpl_tx_hash`, set `settlement_status=completed`, update `wallets.balance_uctusd` **only after**
   on-ledger validation. *(FR-WAL-05, FR-WAL-06)*
9. **Failure handling.** On a `tec` failure, set `settlement_status=failed` with
   `xrpl_error_reason`, leave balance unchanged, and retry per policy or flag for admin.
   *(FR-WAL-07, FR-MQ-05, FR-MQ-06)*

### Acceptance
- Replaying the same settlement message a second time produces **no** additional balance change.
  *(FR-MQ-04)*
- Balance updates only after XRPL validation; a failed transfer never moves the balance and appears
  in history with a reason. *(FR-WAL-06, FR-WAL-07)*
- Every settled transaction row links to a resolvable Testnet hash. *(FR-WAL-05)*

### Tests (`test_cashin.py`, `test_queue.py`, `test_xrpl.py`)
- `test_xrpl.py` **mocks** xrpl-py — no live Testnet in unit tests.
- Double-delivery test asserts single credit.
- Cash-in `failed` asserts no enqueue and no transfer.

**Done when:** a full cash-in→settlement flow completes with a real Testnet hash in the recipient
wallet, and the idempotency test passes.

---

## Phase 7 — Cash-Out with On-Chain Burn  (FR-CO-01..06)  ✅ DONE

**Objective:** recipient converts UCTUSD to USD/ZAR; the platform burns tokens to the issuer and
simulates the fiat payout.

**Depends on:** Phase 6 (recipients hold real balances).

> **Revision note.** The steps below supersede the original Phase 7 draft. Three things changed
> during implementation and are now authoritative: the ZAR payout formula, the definition of
> available balance, and the handling of a burn whose outcome is unknown.

### Steps
1. **Request.** `POST /cashout` with `uctusd_amount` + `target_currency` (USD/ZAR only). Reject
   amounts exceeding available balance. `status=requested`. *(FR-CO-01)*
2. **Payout math — shared with the quote engine.** The helpers live in `fx_service`
   (`cashout_fee_usd`, `cashout_payout`) and `calculate_quote` calls the same code, so a quote's
   estimate and a real cash-out cannot drift:
   - `fee_usd = max(uctusd_amount * cashout_fee_percentage, cashout_fee_min_usd)`
   - USD: `payout = uctusd_amount - fee_usd`  (6 dp)
   - ZAR: `payout = (uctusd_amount - fee_usd) * market_rate`  (2 dp)
   The fee is charged in USD **before** conversion, so ZAR is `(uctusd - fee) * rate`, **not**
   `(uctusd * rate) - fee`. Non-positive payouts are rejected. *(FR-CO-02)*
3. **Pricing is snapshotted, never recomputed.** The rate, fee and payout are persisted on the
   request and approval honours those saved values. The preview the recipient accepted is echoed
   back on submit and compared against a fresh server-side computation; if it moved, a refreshed
   preview is required. Client-supplied figures are only ever compared, never persisted.
4. **Available balance** = `balance_uctusd` − SUM(`uctusd_amount` WHERE `status='requested'`).
   `approved` rows are **not** subtracted — their amount already left `balance_uctusd` at approval,
   so counting them again would double-subtract. The wallet row is locked `FOR UPDATE` across the
   availability check and the insert. *(FR-CO-01)*
5. **Status flow** `requested → approved → completed/failed`, forward-only (no skips/reversals).
   Every transition is a conditional UPDATE guarded on the current status. There is no `processing`
   state: the worker claims with `burn_started_at`. *(FR-CO-03)*
6. **Admin approve** (`require_admin`), in one transaction and in this order: lock the wallet
   `FOR UPDATE` → re-check the balance under the lock → conditional `requested → approved` claim →
   debit → commit. Status and money land together or not at all, and no network call happens inside
   the lock. Publishing the burn message happens after the commit. *(FR-CO-05, FR-CO-06)*
7. **Burn via the queue/worker**, reusing `xrpl_service.burn_to_issuer` — the Phase 5 calls are not
   re-derived. `burn_to_issuer` reads the validated ledger index before signing and hands the caller
   hash + `LastLedgerSequence` + that index **before submitting**; if persistence raises, nothing is
   submitted. The worker's claim requires `status=approved AND xrpl_burn_tx_hash IS NULL`, so once
   anything has been signed **no worker may ever claim the row again**. The full `uctusd_amount` is
   burned; the fee only reduces the simulated fiat payout.
8. **Three post-signing outcomes, and only these:**
   | Outcome | Status | Balance |
   |---|---|---|
   | validated `tesSUCCESS` | `completed`, hash kept, simulated payout written | untouched (already debited) |
   | validated `tec*`/`tef*` (tokens provably did not move) | `failed` + reason | **restored once** |
   | unknown (timeout / connection / no validated result) | stays `approved`, `failure_reason=outcome_unknown`, shown as "Awaiting ledger confirmation" | **stays debited**, never auto-resubmitted |
   Failures before signing are retried with backoff after releasing the claim; on exhaustion the
   row fails and the reserve is restored. Balance never goes negative. *(FR-CO-06)*
9. **Admin Reconcile.** Resolves a held row from the ledger only: validated success → complete once;
   validated failure → fail + restore once; absent → failed **only if** the validated ledger is past
   `LastLedgerSequence` **and** the server's `complete_ledgers` covers
   `[burn_submitted_ledger_index, burn_last_ledger_sequence]`; otherwise unresolved and the hold
   stays. Elapsed wall-clock time is never evidence. Reconcile cannot race the worker into a double
   submit (a hashed row is unclaimable) or a double complete/restore (both go through conditional
   updates). The 10-minute `STUCK_AFTER` claim expiry recovers only a worker that died *before*
   signing.
10. **Re-enqueue sweep.** Approval commits before it publishes, so a crash in between would leave a
    row approved and debited with no message. `cashout_service.sweep_unpublished` finds approved rows
    with no hash, no claim, and an `approved_at` older than `PUBLISH_GRACE`, and re-publishes them.
    It runs at app startup and is safe to run repeatedly — the worker's claim enforces exactly-once.
11. **Migration 0006** creates `cashout_requests` (new table, reusing the `payoutcurrency` enum).
    **UI:** Cash-Out Request with priced preview, Status/History, and detail (recipient); admin
    approval queue with Approve / Reject / Reconcile at `/admin/cashout`, wired to the nav link that
    was previously a dead 404. Fiat payouts are labelled **simulated** throughout. *(FR-CO-04)*

### Acceptance
- Displayed payout equals `(uctusd − fee_usd)` for USD and `(uctusd − fee_usd) × rate` for ZAR, and
  matches what the quote engine estimates. *(FR-CO-02)*
- A failed cash-out restores the reserved balance exactly once; balance never negative. *(FR-CO-06)*
- Completed cash-out has a resolvable burn hash to the issuer.
- An unknown outcome holds the reserve rather than reversing it, and is resolvable by Reconcile.

### Tests (`test_cashout.py`) — 59 tests
- USD/ZAR math, the minimum-fee floor, rounding, and non-positive payouts.
- Over-balance and concurrent requests (real PostgreSQL row locks); approved rows not
  double-subtracted; saved pricing unchanged by a later fee-config edit.
- Duplicate approval and duplicate/concurrent queue delivery debit and burn exactly once.
- Definitive failure restores once; unknown outcome retains the debit; reconcile to success and to
  failure including repeated calls; tx-not-found stays unresolved without proof of expiry.
- Queue-publish failure restores the reserve; worker-crash recovery before and after signing;
  the sweep re-publishes a lost message and is safe to run twice.
- Recipient ownership scoping and admin authorization (403 for non-admins).
- xrpl-py and Redis are faked — no live Testnet.

**Done when:** a recipient cash-out debits, burns on-ledger, and shows `completed` with a hash — and
the failure path restores balance. ✅

---

## Phase 8 — Admin Portal (remaining)  (FR-ADM-01..07)  ✅ DONE

**Objective:** the admin surfaces beyond the KYC queue that already exists.

**Depends on:** `dependencies.py:require_admin` centralized (reconciliation pass); Phases 6–7 for the
data these screens act on.

### Steps
1. **Cash-in confirmation queue** — list pending cash-ins; Confirm → enqueues settlement, Fail →
   cancels + notifies. *(FR-ADM-03, ties FR-CI-02)*
2. ~~**Cash-out approval queue**~~ — ✅ delivered in Phase 7 (`/admin/cashout`: Approve, Reject,
   Reconcile). *(FR-ADM-05, ties FR-CO-05)*
3. **Transaction monitor** — filter by status, date range, user; drill-down showing cash-in status,
   settlement status, XRPL hash, validation result. *(FR-ADM-04)*
4. **Fee & limit config screen** — edit `fee_config` + `limit_tiers` live. *(FR-ADM-06, ties
   FR-FX-08, FR-LIM-05)*
5. **AML flag (could)** — toggle `transactions.aml_flagged`, filterable in the monitor.
   *(FR-ADM-07)*
6. **Gate everything** through `require_admin`; assert sender/recipient roles get 403.
   *(FR-ADM-01)*

### Acceptance / Tests (`test_admin.py`)
- Non-admin receives 403 on every admin route.
- Filters return only matching rows; config edits affect subsequent quotes/limits.

**Done when:** all admin queues function and every admin route is behind `require_admin`. ✅

Delivered: `/admin/transactions` (+ `/admin/transactions/{id}` drill-down) with status, UTC date
range, user and AML filters; the fee editor on `/admin/config` beside the limit tiers; the AML flag
(migration 0007). The cash-in queue, settlement monitor and cash-out queue already existed and were
left as built. `require_admin` coverage is asserted by walking `app.routes` in `test_admin.py`.

---

## Phase 9 — Performance Testing  (Brief §7.iv)

**Objective:** measure and report the numbers the brief asks for, against a running full flow.

**Depends on:** an end-to-end flow (Phases 4–7).

### Steps
1. **Synthetic data.** Faker script → ~200 users with KYC records and beneficiaries, so tests hit
   realistic data. *(Brief: "generate synthetic users")*
2. **Locust file** (`perf/locustfile.py`) with user classes covering: register/login, `GET /quote`,
   full remittance (create → cash-in confirm → settlement), wallet view, cash-out.
3. **Measure & record:** API response times, requests/second, message-queue throughput, UCTUSD
   transaction processing time, success/failure rates, behaviour under concurrent use.
4. **Report** — a small number of tables/charts plus a paragraph on each bottleneck found (e.g.
   Testnet round-trip latency, single-worker queue drain, DB lock contention on balance writes).

### Acceptance
- Report includes every metric in §7.iv with at least one chart, and names the bottlenecks.

**Done when:** the performance section of the deliverable is written from real measured runs, not
estimates.

---

## Cross-cutting reminders (apply in every phase)
- Treasury seed and per-user seeds never appear in logs, API responses, or plaintext DB columns.
- Private keys decrypted only inside the signing path (`xrpl_service.py`).
- Keep the spec document updated **as you build** — each phase produces the content for its spec
  section (settlement flow, cash-out flow, security design, performance results).