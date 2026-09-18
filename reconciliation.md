# Reconciliation Pass — Task Brief for Claude Code

**Goal:** align the existing Slice 1–2 codebase with the corrected plan (UCTUSD, both wallet
addresses, session-based auth, centralized admin gating) **without adding new features**. This is a
cleanup pass that unblocks clean work on Phases 4+.

**Hard constraints**
- Do **not** implement FX, cash-in, queue, XRPL, or cash-out logic here — that's later phases.
- Keep all 29 existing tests green. If a change requires a test update, update the test in the same
  step and explain why.
- No secrets in the repo. Real seeds/keys live in `.env` (gitignored); `.env.example` gets
  placeholders only.
- Keep FR-XXX identifiers stable — never renumber.
- Work in a branch (e.g. `chore/uctusd-reconciliation`) and run `make test` before finishing.

---

## Tasks (in order)

### 1. Verify the on-ledger currency code (do this first — it feeds task 2)
Write a small throwaway script `scripts/check_currency.py` that runs `AccountLines` against the
treasury wallet and prints each line's `currency`, `account` (issuer) and `balance`:

```python
from xrpl.clients import JsonRpcClient
from xrpl.models.requests import AccountLines
client = JsonRpcClient("https://s.altnet.rippletest.net:51234")
resp = client.request(AccountLines(account="rMcBddj7AD6aEFoPSeSL8HpqVJMMxaezoz"))
for line in resp.result["lines"]:
    print(repr(line["currency"]), "issuer:", line["account"], "bal:", line["balance"])
```

Run it, and record the exact `currency` string (it will be `USD` or a 40-char hex value) and confirm
the issuer is `rELez4x4Zqv3KYqboYVfrYPF8521Ycbxa5`. **This value is used verbatim in task 2.**
(If the runner can't reach the Testnet, leave `XRPL_CURRENCY_CODE` as a clearly-marked TODO for a
human to fill in.)

### 2. Config & secrets
- Add these keys to `app/config.py` (Pydantic Settings) and `.env.example`:
  `XRPL_JSON_RPC`, `XRPL_ISSUER_ADDRESS`, `XRPL_CURRENCY_CODE`, `XRPL_PLATFORM_WALLET_ADDRESS`,
  `XRPL_PLATFORM_WALLET_SEED`, `XRPL_ENCRYPTION_KEY`, `REDIS_URL`.
- Fill `.env.example` with the two known addresses and placeholders for the seed/keys/currency.
- **Acceptance:** `Settings` loads without error; no address or currency code is hardcoded anywhere
  in `app/` (grep for the literal addresses — they should appear only in config/`.env.example`).

### 3. RLUSD → UCTUSD terminology
- Replace user-facing strings, comments, docstrings, template text and variable/column names that
  say `RLUSD`/`rlusd` with `UCTUSD`/`uctusd`, **including**:
  `wallets.balance_rlusd → balance_uctusd`, `transactions.rlusd_amount → uctusd_amount`.
- Generate an Alembic migration for the column renames (`make migration name="rlusd_to_uctusd"`).
- Do **not** touch the Fernet/bcrypt security code semantics — this is a naming pass only.
- **Acceptance:** `grep -ri rlusd app/ frontend/` returns nothing except, if present, a historical
  note; migration applies cleanly up and down.

### 4. Auth documentation correction
- The code already uses `SessionMiddleware` (server-side sessions). Ensure no comment, docstring or
  README line claims JWT. Add a one-line note that session auth is a deliberate choice.
- **Acceptance:** `grep -ri jwt .` returns nothing in app code/docs (except an explicit
  "we do not use JWT" note if you add one).

### 5. Centralize admin enforcement (the important one)
- Implement `require_admin` in `app/dependencies.py` as the single admin gate (depends on
  `get_current_user`, raises 403 for non-admins).
- Replace every use of the ad-hoc `_require_admin` helper in `app/routers/admin.py` with
  `Depends(require_admin)`; delete the old helper.
- Confirm **every** admin route (KYC queue, approve/reject, config) goes through it.
- **Acceptance:** `test_admin.py` asserts a non-admin user receives 403 on at least one admin route;
  no admin route bypasses `require_admin`.
- VERIFY and report:
  1. Every admin route, in admin.py AND any other router, is gated with
     Depends(require_admin). List any admin endpoint that ISN'T.
  2. No ad-hoc admin checks remain anywhere — grep for is_admin / role checks that sit
     outside require_admin.
  3. User.is_admin actually exists and is correct.
  4. test_admin.py asserts 403 for a logged-in non-admin, and ideally the /login redirect
     for an anonymous user.
- Flag any issues identified 

### 6. Flag the limit-usage stubs
- In `limit_service.py`, mark `get_daily_usage`/`get_monthly_usage` with a clear
  `# TODO(Phase 4): compute from Transaction model` comment so it isn't forgotten when the
  Transaction model lands. Leave the hardcoded `Decimal("0")` for now.

### 7. README slice table
- Rename the slice scope text from RLUSD to UCTUSD; keep the Done/⬜ statuses as they are.
- Update the "Security Notes" and "Out of Scope" sections if they mention RLUSD.

### 8. Finish
- Run `make test`. All tests pass.
- Summarise, in your final message: the currency code you found (or the TODO you left), the columns
  renamed, and confirmation that all admin routes now route through `require_admin`.

---

## Explicitly out of scope for this pass
- Adding the `cashout_requests.xrpl_burn_tx_hash` column (belongs to Phase 7).
- Any `xrpl_service.py`, `queue_service.py`, `fx_service.py`, worker, or Transaction/wallet/cashout
  model implementation.
- Building the treasury-wallet signing helper (belongs to Phase 5).