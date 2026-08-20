# CLAUDE.md — XRPL FX Remittance Platform

Reference document for Claude Code. Covers locked-in stack decisions and the phased build plan.
Source of truth for implementation choices that must not be revisited without a team decision.

---

## Locked-In Stack

| Layer | Choice | Rationale |
|---|---|---|
| Web framework | **FastAPI** | Async support for XRPL RPC calls and the queue worker; auto-generates OpenAPI docs |
| Database | **PostgreSQL** | Row-level locking (`SELECT FOR UPDATE`) needed for concurrent wallet balance writes from web + worker |
| ORM / migrations | **SQLAlchemy** (async) + **Alembic** | Standard pairing for FastAPI + Postgres |
| Message queue | **Redis + RQ** | Simpler than Celery for a single-queue settlement flow; clear retry/failure semantics |
| Password hashing | **bcrypt** (via `passlib`) | FR-AUTH-02 |
| XRPL private key encryption | **Fernet** (via `cryptography`) | Key stored separately from encrypted blob — FR-WAL-03 |
| XRPL client | **xrpl-py** | Official Python SDK for XRPL Testnet |
| Templates | **Jinja2** (server-rendered) | Served directly by FastAPI via `Jinja2Templates` |

### Confirmed design decisions (from requirements.md §2)
- Each recipient gets a **dedicated, platform-managed XRPL Testnet account** — no pooled wallet.
- TrustSet to RLUSD issuer must be established before any RLUSD transfer.
- Cash-in is **simulated card only** — no real card network.
- Cash-out currencies: **USD and ZAR only**.
- RLUSD balance debit on **approval**, with automatic reversal on failure (FR-CO-06).
- One user account may hold **both sender and recipient roles** (`users.roles` is an array).

---

## Phased Build Order

Do not start a phase until the one above it has passing tests.

```
Phase 0  Foundation
         App skeleton, config (.env), DB session, Alembic init

Phase 1  Auth & Users                  FR-AUTH-01..07
         Registration, login/logout, bcrypt, JWT session

Phase 2  KYC                           FR-KYC-01..05
         Submit form, admin approval, status propagation

Phase 3  Beneficiaries & Limits        FR-BEN-01..05  FR-LIM-01..05
         (parallel — both unblock Phase 4)

Phase 4  FX Quote Engine               FR-FX-01..08
         Rate source, fee/margin calc, GET /quote endpoint

Phase 5  XRPL Wallet Provisioning      FR-WAL-01..04
         Testnet account creation, TrustSet, Fernet key encryption

Phase 6  Cash-In + Queue + Settlement  FR-CI-01..05  FR-MQ-01..06  FR-WAL-05..07
         Simulated card → queue publish → RQ worker → XRPL transfer → validation

Phase 7  Cash-Out                      FR-CO-01..06
         Request, admin approve, balance debit/reversal

Phase 8  Admin Portal                  FR-ADM-01..07
         KYC queue, cash-in queue, cash-out queue, monitor, config screen

Phase 9  UI Templates
         Wire Jinja2 templates onto every router
```

---

## File / Folder Structure

```
xrpl-remittance/
├── app/
│   ├── main.py                   # FastAPI app init, router mounting, lifespan
│   ├── config.py                 # Pydantic Settings (reads .env)
│   ├── database.py               # SQLAlchemy async engine + session factory
│   ├── dependencies.py           # Depends(): get_db, current_user, require_admin
│   │
│   ├── models/
│   │   ├── user.py
│   │   ├── kyc.py
│   │   ├── beneficiary.py
│   │   ├── transaction.py
│   │   ├── wallet.py
│   │   ├── cashout.py
│   │   └── platform_config.py    # fee_config + limit_tiers
│   │
│   ├── schemas/
│   │   ├── auth.py
│   │   ├── kyc.py
│   │   ├── beneficiary.py
│   │   ├── transaction.py        # QuoteRequest / QuoteResponse
│   │   ├── wallet.py
│   │   └── cashout.py
│   │
│   ├── routers/
│   │   ├── auth.py
│   │   ├── kyc.py
│   │   ├── beneficiaries.py
│   │   ├── transactions.py       # quote + send-money flow
│   │   ├── wallet.py             # recipient wallet views
│   │   ├── cashout.py
│   │   └── admin.py
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── kyc_service.py
│   │   ├── beneficiary_service.py
│   │   ├── fx_service.py         # rate fetch + fee/margin/RLUSD calc
│   │   ├── limit_service.py      # daily/monthly cumulative checks
│   │   ├── cashin_service.py     # mock card + status transitions
│   │   ├── cashout_service.py    # request → approved → completed/failed
│   │   ├── queue_service.py      # enqueue settlement messages (RQ)
│   │   └── xrpl_service.py       # account provision, TrustSet, Payment tx
│   │
│   ├── workers/
│   │   └── settlement_worker.py  # RQ job: consume → XRPL transfer → update DB
│   │
│   └── security/
│       ├── crypto.py             # Fernet encrypt/decrypt for XRPL private keys
│       └── hashing.py            # bcrypt hash/verify
│
├── migrations/
│   ├── env.py
│   └── versions/
│
├── tests/
│   ├── conftest.py               # pytest fixtures: test DB, test client, mock XRPL
│   ├── test_auth.py
│   ├── test_kyc.py
│   ├── test_beneficiaries.py
│   ├── test_fx.py
│   ├── test_limits.py
│   ├── test_cashin.py
│   ├── test_queue.py
│   ├── test_xrpl.py
│   ├── test_cashout.py
│   └── test_admin.py
│
├── frontend/
│   ├── templates/
│   │   ├── base.html
│   │   ├── auth/
│   │   ├── sender/
│   │   ├── recipient/
│   │   └── admin/
│   └── static/
│       ├── css/
│       └── js/
│
├── .env.example
├── requirements.txt
├── alembic.ini
└── Makefile                      # targets: dev, worker, test, migrate
```

---

## Data Model

### `users`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| email | VARCHAR UNIQUE | |
| mobile | VARCHAR UNIQUE | |
| full_name | VARCHAR | |
| password_hash | VARCHAR | bcrypt — never plaintext |
| roles | VARCHAR[] | `{sender, recipient, admin}` |
| kyc_status | ENUM | `not_submitted / pending / approved / rejected` |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

### `kyc_submissions`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| user_id | FK → users | |
| full_name | VARCHAR | |
| date_of_birth | DATE | |
| nationality | VARCHAR | |
| id_number | VARCHAR | |
| residential_address | TEXT | |
| mobile | VARCHAR | |
| email | VARCHAR | |
| source_of_funds | TEXT | |
| status | ENUM | `pending / approved / rejected` |
| rejection_reason | TEXT | nullable |
| reviewed_by | FK → users | nullable; admin only |
| reviewed_at | TIMESTAMPTZ | nullable |
| submitted_at | TIMESTAMPTZ | |

### `beneficiaries`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| sender_id | FK → users | |
| recipient_user_id | FK → users | nullable — may not have an account yet |
| full_name | VARCHAR | |
| email | VARCHAR | nullable |
| mobile | VARCHAR | nullable |
| country | VARCHAR | |
| payout_currency | ENUM | `USD / ZAR` |
| relationship | VARCHAR | |
| is_active | BOOLEAN | soft-delete for FR-BEN-05 |
| created_at | TIMESTAMPTZ | |

### `wallets`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| user_id | FK → users UNIQUE | one wallet per recipient |
| xrpl_address | VARCHAR | public |
| encrypted_private_key | TEXT | Fernet-encrypted blob |
| key_encryption_key_id | VARCHAR | pointer into separate key store (env var / Vault) |
| trust_set_complete | BOOLEAN | must be true before any RLUSD send |
| balance_rlusd | DECIMAL(20,6) | platform-side cache; authoritative source is XRPL |
| created_at | TIMESTAMPTZ | |

### `transactions`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| sender_id | FK → users | |
| beneficiary_id | FK → beneficiaries | |
| recipient_wallet_id | FK → wallets | |
| zar_amount | DECIMAL(20,2) | gross send amount |
| exchange_rate | DECIMAL(20,6) | market rate at quote time |
| fx_margin | DECIMAL(6,4) | e.g. 0.02 = 2% |
| transaction_fee | DECIMAL(20,2) | fixed + % combined, in ZAR |
| net_zar_converted | DECIMAL(20,2) | zar_amount − transaction_fee |
| rlusd_amount | DECIMAL(20,6) | net_zar_converted / effective_rate |
| cashout_fee_estimate | DECIMAL(20,6) | preview only, not binding |
| idempotency_key | UUID UNIQUE | queue dedup — 1:1 with this row |
| cashin_status | ENUM | `pending / received / failed` |
| settlement_status | ENUM | `not_queued / queued / processing / completed / failed` |
| xrpl_tx_hash | VARCHAR | nullable; set after ledger confirmation |
| xrpl_validated | BOOLEAN | false until on-ledger validation passes |
| xrpl_error_reason | TEXT | nullable |
| aml_flagged | BOOLEAN | default false |
| created_at | TIMESTAMPTZ | |
| settled_at | TIMESTAMPTZ | nullable |

### `cashout_requests`
| Column | Type | Notes |
|---|---|---|
| id | UUID PK | |
| recipient_id | FK → users | |
| wallet_id | FK → wallets | |
| rlusd_amount | DECIMAL(20,6) | |
| target_currency | ENUM | `USD / ZAR` |
| exchange_rate | DECIMAL(20,6) | rate at request time |
| cashout_fee | DECIMAL(20,6) | |
| net_payout | DECIMAL(20,6) | (rlusd × rate) − fee |
| status | ENUM | `requested / approved / completed / failed` |
| approved_by | FK → users | nullable; admin only |
| approved_at | TIMESTAMPTZ | nullable |
| completed_at | TIMESTAMPTZ | nullable |
| failure_reason | TEXT | nullable |
| created_at | TIMESTAMPTZ | |

### `fee_config` (single active row, admin-editable)
| Column | Type |
|---|---|
| id | PK |
| fixed_fee_zar | DECIMAL |
| percentage_fee | DECIMAL |
| fx_margin | DECIMAL |
| cashout_fee_percentage | DECIMAL |
| updated_by | FK → users |
| updated_at | TIMESTAMPTZ |

### `limit_tiers` (one row per tier)
| Column | Type |
|---|---|
| id | PK |
| tier_name | VARCHAR UNIQUE |
| daily_limit_zar | DECIMAL |
| monthly_limit_zar | DECIMAL |

---

## FR-XXX → Code Module Map

| FR Group | Router | Service | Model | Schema | Test |
|---|---|---|---|---|---|
| FR-AUTH-01..07 | `routers/auth.py` | `auth_service.py` | `user.py` | `schemas/auth.py` | `test_auth.py` |
| FR-KYC-01..05 | `routers/kyc.py` | `kyc_service.py` | `kyc.py` | `schemas/kyc.py` | `test_kyc.py` |
| FR-BEN-01..05 | `routers/beneficiaries.py` | `beneficiary_service.py` | `beneficiary.py` | `schemas/beneficiary.py` | `test_beneficiaries.py` |
| FR-LIM-01..05 | via `routers/transactions.py` | `limit_service.py` | `platform_config.py` | inline transaction schema | `test_limits.py` |
| FR-FX-01..08 | `routers/transactions.py` GET /quote | `fx_service.py` | `platform_config.py` | `schemas/transaction.py` | `test_fx.py` |
| FR-CI-01..05 | `routers/transactions.py` POST + PATCH | `cashin_service.py` | `transaction.py` | `schemas/transaction.py` | `test_cashin.py` |
| FR-MQ-01..06 | n/a (triggered from cashin_service) | `queue_service.py` | `transaction.py` | — | `test_queue.py` |
| FR-WAL-01..07 | `routers/wallet.py` | `xrpl_service.py` | `wallet.py` | `schemas/wallet.py` | `test_xrpl.py` |
| FR-CO-01..06 | `routers/cashout.py` | `cashout_service.py` | `cashout.py` | `schemas/cashout.py` | `test_cashout.py` |
| FR-ADM-01..07 | `routers/admin.py` | (delegates to domain services) | all | all | `test_admin.py` |

---

## Critical Implementation Rules

- **`dependencies.py:require_admin`** is the single enforcement point for all admin-only routes. Every admin router must use it — if it's bypassed, FR-ADM-01 through FR-ADM-07 all fail.
- **`limit_service.py`** must run inside the same DB transaction as the transaction insert (not before it) to prevent a TOCTOU race under concurrent sends from the same user.
- **`settlement_worker.py`** must check `idempotency_key` before processing. The first DB write claiming that key wins; subsequent redeliveries of the same message are a no-op (FR-MQ-04).
- **`security/crypto.py`** is used only by `xrpl_service.py` for key decryption and wallet provisioning — nowhere else. Do not widen this surface.
- **`test_xrpl.py`** mocks `xrpl-py`'s async client — unit tests must not hit the real Testnet. Optional integration tests may, but only in CI with a dedicated Testnet account.
- Plaintext private keys must never appear in logs, API responses, or DB rows — enforced in `xrpl_service.py` and auditable via `security/crypto.py`.
