# XRPL-Based-FX-Remittance-Platform-Using-RLUSD

Here's content for your `README.md` — copy and paste it in:

```markdown
# XRPL-Based FX Remittance Platform

ECO5040W · Financial Software Engineering · University of Cape Town

A simulated cross-border remittance platform built on the XRPL Testnet using RLUSD. Senders in South Africa convert ZAR to RLUSD via a transparent FX quote; recipients hold RLUSD in custodial XRPL wallets and cash out to USD or ZAR.

---

## Tech Stack

| Layer | Choice |
|---|---|
| Web framework | FastAPI (async) |
| Database | PostgreSQL + SQLAlchemy 2.0 + Alembic |
| Message queue | Redis + RQ |
| Blockchain | XRPL Testnet via xrpl-py |
| Key encryption | Fernet (cryptography) |
| Templates | Jinja2 + Bootstrap 5 |

---

## Getting Started

### Prerequisites
- Python 3.11+
- PostgreSQL running locally
- Redis running locally (required from Slice 4 onward)

### Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — set DATABASE_URL, SECRET_KEY, and XRPL_ENCRYPTION_KEY

# 3. Create the database
createdb remittance_db

# 4. Run migrations
make migrate

# 5. Seed the admin user
make seed-admin

# 6. Start the app
make dev
# → http://localhost:8000
```

### Running tests

```bash
createdb remittance_test   # one-time setup
make test
```

---

## User Roles

| Role | Description |
|---|---|
| **Sender** | Registers, completes KYC, adds beneficiaries, initiates ZAR remittances |
| **Recipient** | Receives RLUSD into a custodial XRPL wallet, requests cash-out |
| **Admin** | Approves KYC, confirms cash-ins, approves cash-outs, monitors transactions |

---

## Build Slices

| Slice | Scope | Status |
|---|---|---|
| 1 | Scaffold · Auth (FR-AUTH) · KYC (FR-KYC) · Admin KYC approval | ✅ Done |
| 2 | Beneficiaries (FR-BEN) · Remittance limits (FR-LIM) | 🔨 In progress |
| 3 | FX quote engine (FR-FX) | ⬜ |
| 4 | Simulated cash-in (FR-CI) · Message queue skeleton (FR-MQ) | ⬜ |
| 5 | XRPL standalone integration — account, TrustSet, RLUSD transfer | ⬜ |
| 6 | Settlement worker end-to-end (FR-WAL) | ⬜ |
| 7 | Cash-out (FR-CO) · Remaining admin queues (FR-ADM) | ⬜ |
| 8 | Performance test scripts (brief §7.iv) | ⬜ |

---

## Key Make Commands

```bash
make dev          # Start development server (port 8000)
make worker       # Start RQ settlement worker
make migrate      # Apply pending Alembic migrations
make seed-admin   # Create admin user from .env values
make test         # Run pytest suite
make migration name="describe_change"   # Generate new migration
```

---

## Security Notes

- Passwords are hashed with bcrypt — never stored or logged in plaintext (FR-AUTH-02)
- XRPL private keys are Fernet-encrypted at rest; the encryption key lives in the environment, not the database (FR-WAL-03)
- Private keys are never returned via the API, never logged, and are decrypted only in the signing path (FR-WAL-04)
- Admin routes are enforced at the dependency layer — not just hidden from the UI

---

## Out of Scope

- Real funds, real card processing, or XRPL Mainnet
- Production KYC/AML vendor integration
- Cash-out currencies other than USD and ZAR
```

Update the slice status table as you complete each one.