# CLAUDE.md — XRPL FX Remittance Platform (UCTUSD)

Reference document for Claude Code. Covers locked-in stack decisions and the phased build plan.
Source of truth for implementation choices that must not be revisited without a team decision.

> **Revision note.** This supersedes the original RLUSD-based plan. The project now settles in
> **UCTUSD**, a UCT-issued test IOU. All references to RLUSD are historical. See
> "Settlement asset & wallets" below for the authoritative addresses and the currency-code rule.

---

## Settlement Asset & Wallets

| Thing | Value | Role |
|---|---|---|
| Settlement token | **UCTUSD** (UCT-issued IOU on XRPL Testnet) | The asset moved on-ledger |
| Issuer / burn address | `rELez4x4Zqv3KYqboYVfrYPF8521Ycbxa5` | Issues UCTUSD; withdrawals **burn** tokens by sending them here |
| Platform treasury wallet | `rMcBddj7AD6aEFoPSeSL8HpqVJMMxaezoz` | Pre-funded with UCTUSD; **signs outgoing payments** to recipients |
| Currency code | **Read from the ledger — do not guess** | See rule below |

**Currency-code rule (critical).** XRPL standard currency codes are exactly 3 characters; anything
else is a 160-bit (40-hex-char) value. "UCTUSD" is 6 characters, so on-ledger it is **either** a
40-char hex string **or** the issuer may simply use `USD`. Determine the real value empirically by
reading the trust line on the pre-funded wallet (`AccountLines` on `rMcBddj7…`) and copy the
`currency` field **verbatim** into config. Getting this wrong produces the classic
"TrustSet succeeds but no payment ever finds a path" failure. Never hardcode it.

**Treasury wallet is config, not a table row.** `rMcBddj7…` is the platform's source-of-funds
wallet. It lives in configuration (`XRPL_PLATFORM_WALLET_ADDRESS` + encrypted seed), **not** in
`users` or `wallets`. Its seed is the crown-jewel secret — it controls all UCTUSD liquidity.

---

## Locked-In Stack

| Layer | Choice | Rationale |
|---|---|---|
| Web framework | **FastAPI** | Async support for XRPL RPC calls and the queue worker; auto-generates OpenAPI docs |
| Database | **PostgreSQL** | Row-level locking (`SELECT FOR UPDATE`) needed for concurrent wallet balance writes from web + worker |
| ORM / migrations | **SQLAlchemy** (async) + **Alembic** | Standard pairing for FastAPI + Postgres |
| Message queue | **Redis + RQ** | Simpler than Celery for a single-queue settlement flow; clear retry/failure semantics |
| Session / auth | **Cookie sessions** (`SessionMiddleware`) | Signed browser cookie, no server-side store — **not** JWT; see correction note below |
| Password hashing | **bcrypt** (via `passlib`) | FR-AUTH-02 |
| XRPL private key encryption | **Fernet** (via `cryptography`) | Per-user seed encrypted; key stored separately from blob — FR-WAL-03 |
| XRPL client | **xrpl-py** | Official Python SDK for XRPL Testnet |
| Templates | **Jinja2 + Bootstrap 5** (server-rendered) | Served directly by FastAPI via `Jinja2Templates` |

### Confirmed design decisions
- Settlement token is **UCTUSD** (see table above); issuer address, currency code and platform
  wallet address/seed are all **config values**, never hardcoded constants.
- Each recipient gets a **dedicated, platform-managed XRPL Testnet account** — no pooled wallet.
  The **treasury wallet `rMcBddj7…` sources UCTUSD** to each recipient; per-user liquidity is no
  longer a risk because the treasury is pre-funded.
- A recipient's account needs a **TrustSet to the issuer** before it can receive UCTUSD.
- Same-issuer holder-to-holder movement (treasury → recipient) relies on the issuer having
  **Default Ripple** enabled. We do not configure this (the issuer does); confirm it works with one
  test payment early, and explain it in the spec's settlement section.
- Cash-in is **simulated card only** — no real card network.
- Cash-out currencies: **USD and ZAR only.**
- **Cash-out executes an on-chain burn:** the recipient's account sends UCTUSD back to the issuer
  `rELez4x4…`; the transaction hash is stored, and the fiat conversion is simulated in the DB.
  (This replaces the earlier DB-only status-change model.)
- UCTUSD balance is debited on **approval**, with automatic reversal on failure (FR-CO-06).
- One user account may hold both sender and recipient roles — the User model uses independent boolean flags is_admin, can_send, can_receive (both can_send and can_receive can be true), not a roles array.

### Corrections vs the original plan (now authoritative)
- **Auth is session-based, not JWT.** The code uses `SessionMiddleware`, which stores the session
  in a **signed cookie in the browser** — there is no server-side session store, so a session cannot
  be revoked before it expires (8 h). Calling these "server-side sessions" is wrong; requirements.md
  §9.2 records it as a limitation. Do **not** rip in JWT. Any doc that still says "JWT" is stale.
- **Admin enforcement must be centralized.** `dependencies.py:require_admin` is the single
  enforcement point (see Critical Rules). Admin enforcement is centralized in dependencies.py:require_admin; every admin route uses it and no ad-hoc helper remains. Done.
- **Message queue role.** With per-user accounts, the queue/worker performs the **on-chain
  settlement** (treasury → recipient UCTUSD payment). This is its primary job. (If the team ever
  switched to a platform-wallet model, the queue would instead handle background tasks such as the
  burn-on-withdrawal and notifications — but the per-user model is the chosen path.)

---

## Phased Build Order

Do not start a phase until the one above it has passing tests.

```
Phase 0  Foundation                    ✅ DONE
         App skeleton, config (.env), DB session, Alembic init

Phase 1  Auth & Users                  ✅ DONE   FR-AUTH-01..07
         Registration, login/logout, bcrypt, signed-cookie session

Phase 2  KYC                           ✅ DONE   FR-KYC-01..05
         Submit form, admin approval, status propagation

Phase 3  Beneficiaries & Limits        ✅ DONE   FR-BEN-01..05  FR-LIM-01..05
         Limit usage tracking un-stubbed in Phase 4 — it now sums real
         transactions for the current UTC day/month.

Phase 4  FX Quote Engine               ✅ DONE   FR-FX-01..08
         Transaction model, rate source, fee/margin calc, GET /quote.
         Introduced the Transaction model → limit usage tracking un-stubbed.

Phase 5  XRPL Standalone (de-risk)     ✅ DONE   FR-WAL-01..04
         scripts/xrpl_smoke.py proved the path on Testnet (TrustSet, treasury
         payment, burn, Fernet). Ported to xrpl_service.py + wallets table
         (migration 0004). Observed failure codes: tecPATH_DRY (no trust line),
         tecPATH_PARTIAL (insufficient UCTUSD); bad currency code is rejected
         client-side by xrpl-py. Default Ripple on the issuer confirmed working.

Phase 6  Cash-In + Queue + Settlement  ✅ DONE   FR-CI-01..05  FR-MQ-01..06  FR-WAL-05..07
         Send Money (amount → quote → simulated card) → admin/PATCH confirms cash-in →
         RQ message (idempotency_key) → worker claims key with one conditional UPDATE →
         provision wallet → treasury→recipient payment → credit wallet cache only on
         tesSUCCESS. Hash saved before submit; post-signing failures never auto-retried;
         admin retry/recover checks the ledger first. Recipients must be registered users
         (beneficiary re-linked at send time). Migration 0005. Run the worker with
         `make worker` (SimpleWorker — macOS kills forked RQ work-horses).

Phase 7  Cash-Out (burn)               ✅ DONE   FR-CO-01..06
         Request (priced preview, snapshot persisted) → admin approve (wallet row
         locked, balance debited once) → RQ burn message → worker claims → recipient
         →issuer burn → completed + simulated fiat payout. Three post-signing
         outcomes: validated success completes; a validated tec*/tef* restores the
         reserve once; an UNKNOWN outcome holds the reserve (status stays approved,
         "Awaiting ledger confirmation") and is never auto-resubmitted — an admin
         Reconcile resolves it from LastLedgerSequence + complete_ledgers coverage,
         never from elapsed time. Migration 0006. Worker: cashout_worker.py.

Phase 8  Admin Portal                  ✅ DONE   FR-ADM-01..07
         KYC queue, cash-in queue, settlement monitor and cash-out queue were
         already done (cash-out shipped with Phase 7) and were left untouched.
         Added: transaction monitor /admin/transactions (filters: cash-in status,
         settlement status, UTC date range, user, AML flag) + drill-down
         /admin/transactions/{id} showing both statuses, explorer-linked hash,
         validation result, attempts and the pricing snapshot; the fee editor on
         /admin/config (fixed_fee_zar, percentage_fee, fx_margin,
         cashout_fee_percentage, cashout_fee_min_usd, and the mock
         market_rate_zar_per_usd) alongside the existing limit tiers; the AML
         flag (migration 0007, transactions.aml_flagged) toggled from the
         drill-down and filterable in the monitor. Every /admin route is behind
         require_admin — asserted by a test that walks app.routes.

Phase 9  Performance Tests             ⬜        Brief §7.iv
         Locust scenarios once a full flow runs; synthetic data via Faker.
```

---

## File / Folder Structure

```
xrpl-remittance/
├── app/
│   ├── main.py                   # FastAPI app init, router mounting, lifespan
│   ├── config.py                 # Pydantic Settings (reads .env) — incl. UCTUSD/wallet config
│   ├── database.py               # SQLAlchemy async engine + session factory
│   ├── dependencies.py           # Depends(): get_db, current_user, require_admin  ← single admin gate
│   │
│   ├── models/
│   │   ├── user.py
│   │   ├── kyc.py
│   │   ├── beneficiary.py
│   │   ├── transaction.py        # (Phase 4) ✅
│   │   ├── wallet.py             # (Phase 5) ✅
│   │   ├── cashout.py            # (Phase 7) ✅
│   │   └── platform_config.py    # fee_config + limit_tiers
│   │
│   ├── schemas/                  # auth, kyc, beneficiary, transaction, wallet, cashout
│   ├── routers/                  # auth, kyc, beneficiaries, transactions, wallet, cashout, admin
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── kyc_service.py
│   │   ├── beneficiary_service.py
│   │   ├── fx_service.py         # rate fetch + fee/margin/UCTUSD calc  (Phase 4) ✅
│   │   ├── limit_service.py      # daily/monthly cumulative checks
│   │   ├── cashin_service.py     # mock card + status transitions  (Phase 6)
│   │   ├── cashout_service.py    # request → approved → burn → completed/failed  (Phase 7)
│   │   ├── queue_service.py      # enqueue settlement messages (RQ)  (Phase 6)
│   │   └── xrpl_service.py       # account provision, TrustSet, Payment, burn  (Phase 5/6/7)
│   │
│   ├── workers/
│   │   ├── settlement_worker.py  # RQ job: consume → UCTUSD transfer → update DB  (Phase 6)
│   │   └── cashout_worker.py     # RQ job: claim → burn → complete/reverse/hold  (Phase 7) ✅
│   │
│   └── security/
│       ├── crypto.py             # Fernet encrypt/decrypt for XRPL private keys
│       └── hashing.py            # bcrypt hash/verify
│
├── migrations/ · tests/ · frontend/{templates,static}
├── .env.example · requirements.txt · alembic.ini · Makefile
```

---

## Configuration (.env keys)

```
# Core
DATABASE_URL=postgresql+asyncpg://...
SECRET_KEY=...                       # session signing
REDIS_URL=redis://localhost:6379/0

# XRPL / UCTUSD
XRPL_JSON_RPC=https://s.altnet.rippletest.net:51234
XRPL_ISSUER_ADDRESS=rELez4x4Zqv3KYqboYVfrYPF8521Ycbxa5
XRPL_CURRENCY_CODE=<exact code from AccountLines on the treasury wallet — verbatim>
XRPL_PLATFORM_WALLET_ADDRESS=rMcBddj7AD6aEFoPSeSL8HpqVJMMxaezoz
XRPL_PLATFORM_WALLET_SEED=<treasury seed — obtain from lecturer; NEVER commit>

# Key management
XRPL_ENCRYPTION_KEY=<Fernet key for per-user seeds — separate store from the DB>
```

Switching between the official issuer and any fallback IOU must remain a **config change, not a
rewrite** (brief §4). The currency code and both addresses are the only things that change.

---

## Data Model

Unchanged tables from the original plan: `users`, `kyc_submissions`, `beneficiaries`, `wallets`
(one row per recipient: `xrpl_address`, `encrypted_private_key`, `key_encryption_key_id`,
`trust_set_complete`, `balance_uctusd` cache), `transactions`, `fee_config`, `limit_tiers`.

Changes for UCTUSD:
- **`wallets.balance_rlusd` → `balance_uctusd`** (platform-side cache; authoritative source is XRPL).
- **`transactions.rlusd_amount` → `uctusd_amount`**; `xrpl_tx_hash` holds the treasury→recipient
  settlement hash.
- **`cashout_requests`** (created by migration 0006, Phase 7): `uctusd_amount`, `target_currency`
  (reuses the existing `payoutcurrency` enum — exactly USD/ZAR), the pricing snapshot
  (`market_rate`, `cashout_fee_percentage`, `cashout_fee_min_usd`, `cashout_fee_usd`,
  `net_payout`), `status` (`cashoutstatus`: requested/approved/completed/failed),
  `approved_by` + `approved_at`, `idempotency_key` (UUID UNIQUE), `xrpl_burn_tx_hash` (nullable),
  the reliable-submission recovery pair `burn_last_ledger_sequence` +
  `burn_submitted_ledger_index`, the worker claim flag `burn_started_at` + `burn_attempts`,
  `failure_reason`, and the simulated payout fields `fiat_payout_reference` + `fiat_paid_at`.
  There is deliberately **no `processing` status** — FR-CO-03 names four states, so the worker
  claims a row with `burn_started_at` rather than inventing a fifth.
- **Treasury wallet is NOT a row** — it is config (see above).

- **`transactions.aml_flagged`** (added by migration 0007, Phase 8): NOT NULL boolean,
  `server_default false`, indexed. An admin review marker only — set from the transaction monitor,
  filterable there, and it never changes cash-in state, settlement state or any balance.

Keep `transactions.idempotency_key` (UUID UNIQUE) as the queue-dedup / anti-double-credit key.

### `users`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| email | VARCHAR(255) UNIQUE | indexed |
| mobile | VARCHAR(50) UNIQUE | |
| full_name | VARCHAR(255) | |
| password_hash | VARCHAR(255) | bcrypt — never plaintext |
| is_admin | BOOLEAN | default false |
| can_send | BOOLEAN | default true |
| can_receive | BOOLEAN | default false — both can_send & can_receive may be true (dual role) |
| kyc_status | ENUM `kycstatus` | not_submitted / pending / approved / rejected |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

No `roles` array — role membership is the three booleans above.

---

## FR-XXX → Code Module Map

| FR Group | Router | Service | Model | Test |
|---|---|---|---|---|
| FR-AUTH-01..07 | `routers/auth.py` | `auth_service.py` | `user.py` | `test_auth.py` |
| FR-KYC-01..05 | `routers/kyc.py` | `kyc_service.py` | `kyc.py` | `test_kyc.py` |
| FR-BEN-01..05 | `routers/beneficiaries.py` | `beneficiary_service.py` | `beneficiary.py` | `test_beneficiaries.py` |
| FR-LIM-01..05 | via `routers/transactions.py` | `limit_service.py` | `platform_config.py` | `test_limits.py` |
| FR-FX-01..08 | `routers/transactions.py` GET /quote | `fx_service.py` | `transaction.py` | `test_fx.py` |
| FR-CI-01..05 | `routers/transactions.py` POST + PATCH | `cashin_service.py` | `transaction.py` | `test_cashin.py` |
| FR-MQ-01..06 | (triggered from cashin_service) | `queue_service.py` | `transaction.py` | `test_queue.py` |
| FR-WAL-01..07 | `routers/wallet.py` | `xrpl_service.py` | `wallet.py` | `test_xrpl.py` |
| FR-CO-01..06 | `routers/cashout.py` | `cashout_service.py` | `cashout.py` | `test_cashout.py` |
| FR-ADM-01..07 | `routers/admin.py` | (delegates to domain services) | all | `test_admin.py` |

---

## Critical Implementation Rules

- **`dependencies.py:require_admin`** is the single enforcement point for all admin-only routes.
  Every admin router must use it. 
- **Currency code is read from the ledger, never hardcoded.** `xrpl_service.py` uses
  `settings.XRPL_CURRENCY_CODE` for every `TrustSet` and `IssuedCurrencyAmount`.
- **`limit_service.py`** must run inside the same DB transaction as the transaction insert (not
  before it) to prevent a TOCTOU race under concurrent sends from the same user.
  `get_daily_usage`/`get_monthly_usage` were un-stubbed in Phase 4 and now sum real
  `transactions` rows for the current UTC day/month (a `failed` cash-in does not consume
  allowance).
- **`settlement_worker.py`** must check `idempotency_key` before processing. The first DB write
  claiming that key wins; subsequent redeliveries of the same message are a no-op (FR-MQ-04).
- **Cash-out burn** submits a real Payment from the recipient's account to the issuer `rELez4x4…`,
  stores `xrpl_burn_tx_hash` **before submitting**, and only then marks the (simulated) fiat payout
  complete in the DB. The **full `uctusd_amount` is burned**; the cash-out fee is deducted only when
  computing the fiat payout, never from the burn.
- **Cash-out money rules (FR-CO-06).** `balance_uctusd` is debited exactly once, at approval, inside
  the same transaction as the `requested → approved` claim and while the wallet row is held
  `FOR UPDATE`. It is restored only by the single conditional `approved → failed` transition, so a
  reversal cannot fire twice. Restores only add, so the balance can never go negative. A burn whose
  outcome is **unknown must never be reversed** — that would credit back UCTUSD that may already be
  destroyed on-ledger. Available balance for a new request = `balance_uctusd` minus the sum of
  **`requested`** rows only; `approved` rows have already left the balance.
- **Never treat elapsed time as proof of a failed burn.** Reconcile may only fail-and-restore when
  the transaction is absent *and* the validated ledger is past its `LastLedgerSequence` *and* the
  server's `complete_ledgers` covers the whole submission range. The 10-minute `STUCK_AFTER` claim
  expiry is a liveness check for a dead worker only, and applies solely to rows that never reached
  signing (`xrpl_burn_tx_hash IS NULL`). Once a row has a hash, no worker may ever re-claim it.
- **Late-bind queue publishers** in cash-out (`enqueue or queue_service.enqueue_burn` inside the
  function). A default argument binds the function object at import time, which silently defeats
  substitution in tests and publishes real jobs.
- **`security/crypto.py`** is used only by `xrpl_service.py` for key decryption and wallet
  provisioning — nowhere else. Do not widen this surface.
- **The treasury seed and per-user seeds** must never appear in logs, API responses, or DB rows in
  plaintext — enforced in `xrpl_service.py`, auditable via `security/crypto.py`.
- **`test_xrpl.py`** mocks `xrpl-py`'s client — unit tests must not hit the real Testnet. Optional
  integration tests may, but only against the dedicated Testnet accounts above.
