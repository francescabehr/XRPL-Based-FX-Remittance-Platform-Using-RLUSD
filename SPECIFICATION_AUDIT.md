# Specification-to-code audit

Audit date: 22 September 2026. Repository commit: `6ee6a1b19b37988729cc87b3d7c9cc673550c444`.

Document audited: the attached **Business and Technical Specification — XRPL-Based FX Remittance Platform (UCTUSD), second draft, version 0.2**, supplied as `pasted-text.txt`. This report audits that attachment, rather than treating the repository's older `SPECIFICATION.md` as the specification to satisfy.

**Conclusion: the document does not match the code exactly.** Its principal architecture, default fee calculations, roles, routes, and ordinary transaction flows are substantially accurate. Several statements overstate the implementation's guarantees; some performance statements conflict with the saved data; and the draft contains unfinished sections and a contradictory replacement failure table.

## Scope and evidence

Read: all Python source under `app/`, all seven migration revisions and migration configuration, all frontend templates and the stylesheet, all test modules and fixtures, all `perf/*.py` scripts, the saved performance report and CSV/JSON data, both ledger utility scripts, `README.md`, `requirements.md`, `SPECIFICATION.md`, `CLAUDE.md`, `BUILD_PLAN.md`, `reconciliation.md`, `Makefile`, dependency/configuration files, `.env.example`, and `.gitignore`. The saved chart images were not visually audited; numerical conclusions below use their source data and chart-generation code.

Evidence levels used below:

- **Confirmed:** directly established from source, collected test cases, or saved data.
- **Offline reproduced:** executed the relevant calculation locally without a database, Redis, or ledger call.
- **Static risk:** a problematic path is visible in source, but the described concurrent/crash scenario was not executed.
- **Unverified:** the supplied files cannot independently establish the claim.

No application code, configuration, database records, or ledger balances were changed. No live payment, burn, provisioning, load test, or network verification was performed. Private `.env` values were not printed or independently audited. Historical git contents were not searched for secrets. Therefore, this is not a certification of deployed configuration, historical secret handling, legal compliance, or current ledger state.

Validation performed:

- `DEBUG=false .venv/bin/python -m pytest --collect-only -q`: **212 test cases collected**. The per-module counts exactly match §12.4.
- Without the process-only override, collection failed because the environment supplied `debug='release'`, which is not a valid boolean. No configuration file was edited.
- Direct offline calls reproduced the worked example, cost table, fee-floor comparison, and a rounding discrepancy described below.
- Recomputed aggregate request and queue throughput figures from saved CSVs.
- Inspected the loaded route table: every `/admin` route directly depends on `require_admin`.

**The full suite was not executed.** `tests/conftest.py:25` creates application tables and drops them at session teardown using the configured test database. Collection establishes the test count, not that 212 tests pass. The fixture creates tables from ORM metadata, so even a successful ordinary test run would not itself verify the Alembic migration sequence.

## Findings requiring substantive correction

### 1. Rate re-verification is optional, and only the effective rate is compared

**Document:** §§3–5, 7.1 and 13 describe payment as refused whenever the quoted rate moved, and imply acceptance of the reviewed terms.

**Confirmed:** `app/routers/transactions.py:223` gives the submitted `exchange_rate` a default empty string. `_parse_amount` turns empty, invalid or non-positive values into `None`; `app/services/cashin_service.py:93` compares rates only when `expected_exchange_rate is not None`. A client can omit the rate and still reach transaction creation at the current server price. The ordinary HTML form does provide the rate.

Only `exchange_rate` is compared. A change to the fixed fee or percentage fee can reduce the UCTUSD received without changing that rate, and submission will use the new fee without a price-change refusal. Furthermore, the review-page link carries amount and beneficiary, not the reviewed rate; `/send/pay` generates another quote. See `frontend/templates/sender/send_review.html` and `send_pay.html`.

**Correction:** describe server-side repricing and an optional effective-rate comparison accurately, or require the reviewed terms and compare all economically relevant figures. Prices are calculated server-side; this finding concerns consent to changed terms, not trusting a client-supplied payout.

### 2. Quotes are not saved when generated, and transactions do not snapshot every quote figure

**Document:** §5.2 says the rate is captured on the transaction at quote generation; §§5.1 and 8 imply every transaction stores all its pricing terms.

**Confirmed:** `quote_for` returns an in-memory `Quote`; the row is created later by `create_remittance`. `app/models/transaction.py:56` stores gross amount, total transaction fee, net ZAR, market/effective rates, margin and UCTUSD. It does **not** store the fixed-fee parameter, percentage-fee parameter, cash-out fee parameters, estimated cash-out fee, payout estimate, or selected payout currency.

Cash-out requests have their own later pricing snapshot (`app/models/cashout.py:68`, `app/services/cashout_service.py:94`). They use the then-current fee configuration and rate, so the sender's original payout estimate is not a promise of the eventual cash-out amount. Shared calculation functions ensure agreement for identical inputs, not across different dates/configurations.

**Correction:** distinguish a transient quote, the remittance snapshot at payment submission, and the separate cash-out snapshot at request creation. Stored remittance amounts remain unchanged after fee edits, but the full original quote cannot be reconstructed from that row alone.

### 3. The balance is not written only after ledger validation

**Document:** §8 says `wallets.balance_uctusd` is written only after on-ledger validation.

**Confirmed:** settlement credits follow success, but cash-out approval subtracts the reserve before signing (`app/services/cashout_service.py:279`). Queue-publish failure or exhausted pre-signing retries can restore it without a ledger transaction (`app/services/cashout_service.py:216`; `app/workers/cashout_worker.py:158`, `:222`).

**Correction:** call this the platform balance after approved cash-out reservations. Explain that it can legitimately differ from the ledger while a burn is pending. Available balance subtracts outstanding **requested** cash-outs as well (`cashout_service.py:123–146`). Preserve the narrower, accurate guarantee that incoming settlement credits require validated success.

### 4. Cash-out recovery is a startup sweep, not continuous recovery of every lost job

**Document:** §7.3 says a sweep republishes any approved row whose publish was lost.

**Confirmed:** the only application caller of `sweep_unpublished` is startup in `app/main.py:18`. The query requires approval older than two minutes, no hash, and **no claim** (`cashout_service.py:368`). No periodic caller exists in the read application or operational scripts.

A restart inside the grace period skips the row, and there is no later automatic sweep in that running process. A worker that died before signing has a non-null `burn_started_at`; the sweep excludes it even when stale. The worker can reclaim a stale unsigned row **if another message arrives**, but nothing here guarantees that message. The crash-recovery test directly invokes the worker again; it does not demonstrate automated redelivery.

**Correction:** document startup-only coverage, grace-period exclusions, and the remaining delivery requirement, or implement periodic recovery. Do not describe the helper's existence as guaranteed eventual recovery.

### 5. A crash after persisting a burn hash can leave no Reconcile button

**Confirmed static path:** the worker commits the hash before submission (`app/workers/cashout_worker.py:256`). If the process dies before recording `outcome_unknown`, the approved row retains a hash but no unknown-outcome reason. `CashOutRequest.awaiting_ledger_confirmation` requires that reason prefix (`app/models/cashout.py:129`). `frontend/templates/admin/cashout_queue.html` exposes Reconcile only when that property is true; otherwise it says “Burn in progress…”.

The POST reconcile route can handle an approved hashed row, but the queue does not offer its button in this crash state. A hash also blocks worker reclaim, regardless of age.

**Correction:** qualify the claim that all unknown burns are presented for reconciliation, or expose recovery for stale approved rows with a hash. This crash scenario was not executed during this audit.

### 6. Settlement retry does not prove that an earlier payment expired

**Document:** §7.4 promises refusal while a transaction could still validate. The appended policy mentions a five-minute margin.

**Confirmed:** `app/services/cashin_service.py:244` queries the hash, reconciles `tesSUCCESS`, and otherwise permits a missing result once `updated_at` is five minutes old. The transaction model has no settlement `LastLedgerSequence` or submission-ledger range. `get_transaction_result` also returns `None` for any unsuccessful RPC response, not solely `txnNotFound` (`xrpl_service.py:194`).

Cash-out reconciliation uses ledger sequence and history coverage; settlement reconciliation does not. The implementation therefore matches the appended *time-based rule*, but not the stronger assurance that expiry/non-inclusion has been established. A historical success unavailable from the queried server can be treated as retryable. This is a static risk, not an observed duplicate transfer.

**Correction:** explicitly disclose the settlement heuristic and distinguish it from cash-out recovery; stronger guarantees require ledger-based settlement recovery metadata and checks.

### 7. The appended retry table contradicts both §7.4 and the worker

**Confirmed:** a treasury Payment returning `tecPATH_DRY` is failed, with no automatic retry. `tests/test_queue.py:test_ledger_failure_is_recorded_and_not_retried` explicitly covers it. The appended replacement table describes this code as transient/requeued.

A wallet whose provisioning leaves `trust_set_complete=False` does trigger a retry before the treasury Payment. That is a different branch. Also, “nothing reached the ledger before signing” must refer specifically to the **settlement Payment**: provisioning can already have submitted a TrustSet, and that TrustSet can be retried. `app/services/xrpl_service.py:209` and `app/workers/settlement_worker.py:184` establish the distinction.

**Correction:** retain §7.4's terminal Payment-code handling and separate provisioning retries from Payment retries. Remove the contradictory appended replacement text.

### 8. Three total attempts use delays of 10 and 30 seconds, not 10, 30 and 60

**Confirmed:** both workers set `MAX_ATTEMPTS = 3` and define `(10, 30, 60)`, but fail at attempt 3 before scheduling another retry. Only the first two delays are reachable (`settlement_worker.py:141`; `cashout_worker.py:222`).

**Correction:** “At most three total attempts: initial attempt, retry after 10 seconds, retry after 30 seconds.” The unused 60-second constant does not establish a third retry.

### 9. Post-signing failures do not always use `outcome_unknown`

**Confirmed:** exceptions that escape submission are converted by the settlement worker to `outcome_unknown` once a hash exists. However, `xrpl_service.submit` catches `XRPLException` and can return an `XRPLResult` with `submission_error`; the worker stores that code through its generic failure branch (`xrpl_service.py:170`; `settlement_worker.py:191–208`).

**Correction:** describe both post-signing error representations. Both avoid automatic settlement resubmission, but the appended table's exact failure-code statement is too narrow.

### 10. “Validated failure” is not reliably preserved in the XRPL result interface

**Confirmed source limitation:** `xrpl_service._result_code` extracts a code by regex from exception text. `XRPLResult` carries no field distinguishing a final ledger result from a preliminary code. The cash-out worker holds `submission_error` but restores for other non-success results (`app/workers/cashout_worker.py:290`).

The locally installed SDK's `xrpl/asyncio/transaction/reliable_submission.py` raises exceptions both for a validated failure and for expiry messages containing a **preliminary** result; it also raises for preliminary `tem` errors. The adapter removes that distinction. Therefore, the claim that every code reaching the restore branch represents an independently confirmed validated failure is stronger than the interface supports.

**Correction:** preserve finality/provenance in the result object or re-query before classifying a signed failure as definitive. This audit inspected that installed SDK path; it did not reproduce a live erroneous restoration.

### 11. “Database compromise cannot move funds” exceeds the actual security boundary

**Document:** §10's controlling principle includes both protecting private keys and preventing movement of funds following database compromise.

**Confirmed distinction:** encryption protects seeds against a **read-only database dump**, assuming the separate key remains secret. Database write access is different: users' admin flags, transaction recipient/amount/status fields, cash-out requests, and cached balances are mutable database state consumed by the web app/workers (`dependencies.py:14`, `:38`; both worker `claim` functions). There is no independent signed approval record or integrity check binding those fields to a trusted origin.

**Correction:** scope the demonstrated protection to disclosure of usable private keys from a database dump. Do not claim protection against arbitrary database writes moving funds through the authorized application. No exploitation was attempted.

### 12. Session expiry is renewable, not a fixed eight-hour maximum from login/theft

**Confirmed:** `app/main.py:35` configures `max_age=28800`. The inspected installed Starlette middleware re-signs a nonempty session and issues a fresh cookie on responses. A copied cookie is usable until its timestamp expires, but someone using it can receive renewed cookies. Logout only clears the current browser's session.

**Correction:** specify an eight-hour cookie age with renewal on activity, no server-side revocation, and no implemented absolute session lifetime. The draft's implication that a stolen active session necessarily dies within eight hours is too strong. Also, the application does not set `https_only=True`; the session cookie's Secure flag is absent under the inspected middleware defaults. A proxy providing TLS alone does not change that application setting.

## Other confirmed implementation/document differences

| ID | Document claim | Read implementation and correction |
|---|---|---|
| 13 | §§3/5.5: KYC and limits share one enforcement path because unapproved users have R0. | `require_approved_sender`, `_sender_redirect`, and `create_remittance` independently check KYC/can_send. The R0 tier is a separate limit rule. Both tiers are admin-editable. State that explicit KYC gates coexist with seeded R0 limits. |
| 14 | §3: cash-in confirmation is the only event capable of publishing settlement. | It is the initial gate, but `retry_settlement`, `requeue_stuck`, and worker backoff also publish. The worker claim additionally checks `cashin_status=received`. Describe the necessary state condition rather than one exclusive publishing event. |
| 15 | §9: 401 unauthenticated, 403 forbidden, 404 missing, 409 illegal transition as general conventions. | `/quote` uses 401; anonymous admin access, including the JSON PATCH, uses 302 via `require_admin`. Portal missing/unauthorized object views usually redirect; failed form operations often return 200 or 302. Scope codes to specific endpoints and document framework validation responses separately. |
| 16 | §5: all rounding is half-up. | **Offline reproduced:** `cashout_service.parse_amount('10.1234565')` returns `10.123456`; half-up is `10.123457`. Its `quantize` omits `rounding`, unlike the FX helpers. State the exception or change parsing. |
| 17 | §§5.2/8: rates and margins are all `NUMERIC(20,6)` and ZAR is always `(20,2)`. | Rates are `(20,6)`, but percentage fees/margins are `(10,6)` in models and migrations. `cashout_requests.net_payout` is `(20,6)` for both currencies, although ZAR values are calculated to two decimal places. |
| 18 | §5: displayed and stored figures agree exactly in precision. | Sender quote templates show rates to four decimal places and cash-out estimate/USD payout to two; stored/calculated rates and token/USD figures can have six. For the worked example the sender sees `$49.87`, while the calculation is `49.874404`. Distinguish calculation precision from display precision. |
| 19 | §5.1: four configurable parameters plus market rate. | The editor accepts **five fee/margin parameters** plus market rate: fixed fee, percentage fee, margin, cash-out percentage, cash-out minimum. Four fee categories is defensible, four parameters is not. |
| 20 | §§5/8: one active fee row. | One is seeded, and the editor updates the selected row. No unique constraint enforces one active row; `get_active_fee_config` chooses the newest active row. Describe a convention, not a database invariant. |
| 21 | §8: beneficiary database column is `relation_type`. | `app/models/beneficiary.py:38` maps Python attribute `relation_type` to database column **`relationship`**, also used in migration 0002. Label ORM attributes and SQL columns separately. |
| 22 | §8: latest KYC submission drives user KYC status. | Display selection uses the latest submission, but `approve_kyc`/`reject_kyc` update the user from **any submission passed in**, without checking latest/pending. Admin routes accept old submission IDs and their detail page still shows decision buttons. An older decision can override the user while the latest submission remains different. |
| 23 | §§6/10: everything that signs runs through a worker, and crypto has one importing module. | True of ordinary application signing call sites and crypto imports **within `app/`**. `scripts/xrpl_smoke.py` and `perf/measure_xrpl.py` perform ledger operations directly; performance scripts and tests also import crypto. Scope the statement to the web application's normal transaction flow. Web settings also load secrets; this is a code-path separation, not separately enforced process access to keys. |
| 24 | §6: ledger delays become backlog rather than user-facing delay/error. | `/wallet` waits up to five seconds for a live balance. Admin retry/reconcile also query the ledger and report failures. Narrow the statement to asynchronous payment submission. |
| 25 | §6: changing the asset is only a configuration change. | New ledger operations use configured currency/issuer, but UI/schema terminology remains UCTUSD. Existing wallets retain `trust_set_complete=True` without recording which issuer/currency it applied to, and existing balances/transactions have no per-record asset identifier. Qualify this as configuration of a fresh deployment's settlement asset; changing an active installation requires handling existing trust lines and balances. |

Additional details worth making explicit: registration logs the user in immediately; `can_receive` is set when a wallet is provisioned, before the first payment necessarily succeeds; card validation includes Luhn, expiry and a special declined card, not just field shapes. These are visible in `auth.py`, `xrpl_service.provision_wallet`, and `cashin_service.validate_mock_card`.

## Performance and test claims

### 26. Saved HTTP data contains 6,646 requests, not 6,647

`perf/results/run_stats.csv` has aggregate request count **6646**, zero reported failures, **36.9749366 requests/s**, median **15 ms**, p95 **330 ms**. The report's detailed table agrees with 6646; its summary and the attachment say 6647. The final history sample records 6481, not 6647. There could have been a later console count, but no such evidence was read.

**Correction:** use the saved aggregate, or retain the missing final-run evidence and explain the discrepancy. The rounded 37 requests/s and reported median/p95 are supported.

### 27. A third endpoint fails the stated sub-second p95 target

The draft excludes only login and wallet. Saved p95 values are:

| Request | p95 |
|---|---:|
| POST /login | 7,800 ms |
| GET /send | **2,900 ms** |
| GET /wallet | 1,300 ms |

`perf/REPORT.md` attributes `/send` to login-time blocking, but that explanation does not make the measurement sub-second. Most other endpoints also have individual multi-second maxima. Define the target percentile before claiming it is met; the results do not establish that every interaction is sub-second.

### 28. Four workers do not solve the reported sustained backlog

The CSVs reproduce **9.2079 settlements/minute** for one worker and **37.2935/minute** for four. At the draft's approximately five confirmed cash-ins per second, arrivals would be about **300/minute**, far above 37.3. Four workers improve capacity but cannot keep up with that arrival rate.

The four-worker file measures draining an existing backlog: queued jobs fall from 825 to 746 over 127.1 seconds, with 79 completions. It does not measure four workers keeping up with the original arrival stream. The one-worker file spans 202 seconds, not exactly the 180-second Locust duration; label measurement windows separately.

Also, HTTP confirmations are not necessarily distinct cash-ins: the test profile can submit repeated admin confirmations, and 997 confirmation requests exceed 855 remittance-creation requests. Avoid treating all 997 requests as newly accepted cash-ins without transaction-state evidence.

### 29. Real multiworker throughput is an extrapolation

`perf/analyze.py:105` calculates real-ledger throughput as `60 / payment_mean` and multiplies by four; the 4/minute and 17/minute figures were not measured with real workers. The simulated worker bypasses actual signing and therefore cannot validate multiworker behavior of the shared treasury signer.

**Correction:** label these as idealized estimates for already-provisioned recipients, excluding provisioning, queue delay, shared-signer coordination, and network effects. Retain the accurate qualification that only three real payment timings are recorded.

### 30. “One admin” and “zero HTTP failures” need narrower wording

`AdminUser` uses `weight=1`, not `fixed_count=1`, in a 6:3:1 weighted profile. One admin **account** is configured, but the script does not enforce one concurrent admin client. Saved request statistics do not record the per-class population.

The recipient task explicitly marks HTTP 400 preview responses successful. Its requested amount is 1 UCTUSD; with the seeded USD 1 minimum, payout is zero and the preview rejects even if the wallet is funded. The CSV has **no POST /cashout requests**, approvals, or burns. Thus zero Locust-recorded failures is not proof of zero HTTP error responses or a successful cash-out load path.

The simulated worker replaces provisioning and treasury payment only. Wallet reads still call the real ledger, and cash-out burns are not mocked by that worker. Scope “simulated ledger” to the settlement operations actually substituted.

### 31. No lock-wait instrumentation supports “no lock contention measured”

The sampler records queue/job counts and transaction status counts, not database lock waits. Endpoint latency can support “no apparent lock-related slowdown in this workload,” but cannot independently establish an absence of lock contention.

Duplicate delivery and concurrent limit/approval cases are present in unit tests. The performance CSVs do not independently prove exactly-once ledger behavior. Keep those evidence sources separate.

### 32. The counts are correct; “59 implemented and tested” is too categorical

Recounting `requirements.md` gives **61 requirements: 49 Must, 10 Should, 2 Could**. Collection gives the claimed test distribution:

| Area | Collected cases |
|---|---:|
| Cash-out | 59 |
| XRPL and crypto | 32 |
| Cash-in | 25 |
| Admin | 23 |
| Queue/worker | 21 |
| Auth, KYC, beneficiaries, limits, FX, display | 52 |
| Total | **212** |

However, counting tests does not prove every acceptance criterion. Examples visible in the read code:

- **FR-AUTH-01 / FR-KYC-01 / FR-BEN-01:** string inputs lack comprehensive format/nonblank validation. Registration accepts malformed email/mobile strings and whitespace names; KYC validates age and ID length but not all fields' contents; beneficiary validation checks raw contact truthiness before stripping. Several forms use `novalidate`. These do not meet the literal “all required fields validated”/“no missing or invalid fields” claims.
- **FR-AUTH-03:** clearing a cookie does not invalidate a copied session; the companion requirements acknowledge this deviation.
- **FR-WAL-05:** incoming transactions appear on `/wallet`; outgoing cash-outs are deliberately on a separate history page. If “wallet screen shows incoming/outgoing” is literal, record that UI deviation.
- **FR-CO-03:** rejection allows `requested → failed`, bypassing approved. The attachment accurately shows this, while the literal requirement says “no skipped … states.” Amend the acceptance wording rather than claiming exact conformance.
- **FR-ADM-04:** the filterable transaction monitor covers remittances, with a 500-row cap. Cash-outs are separate; the admin cash-out list excludes terminal requests. Clarify what “all transactions” means.

FR-AUTH-04's absent profile editor and FR-BEN-04's lack of invites/placeholders are correctly acknowledged. A defensible statement is that the major flows have implementation and test coverage, with an explicit acceptance-criterion deviation register. This audit does not invent a replacement implemented-total from test counts.

## Additional code issues relevant to the document's guarantees

These findings were found while tracing the stated controls. They are not claims that the failures occurred in the recorded deployment.

**33. Small positive sends can create non-positive settlement amounts — offline reproduced calculation, confirmed validation gap.** A seeded R1 quote produces fee R25.02, net ZAR −R24.02, UCTUSD −1.272920 and estimated payout −USD 2.272920. The HTML input allows R1; the route and creation service do not require positive net UCTUSD or payout. The ledger amount builder eventually rejects non-positive UCTUSD, after a transaction/cash-in can have been accepted. Document this limitation or reject uneconomic sends before simulated payment. Sources: `fx_service.py:148`, `transactions.py:105`, `cashin_service.py:93`, `xrpl_service.py:99`.

**34. Locked cash-out writes can use an already-loaded wallet value — static concurrency risk.** Admin `get_request` eagerly loads `CashOutRequest.wallet` before `approve` takes its wallet lock. The locked select does not use `populate_existing` or explicit refresh, and sessions use `expire_on_commit=False`. Two requests that loaded the same wallet before either obtained the lock can perform absolute Python-calculated balance assignments using stale ORM state. Row locking alone does not refresh an existing identity-map object's attributes. The existing concurrent approval test races the same cash-out row, whose conditional state transition prevents the second debit; it does not demonstrate distinct cash-out approvals or approval versus incoming credit. Inspect `cashout_service.py:247`, `:279`, `:410` and `database.py:12`. Validate this path with a targeted PostgreSQL concurrency test before retaining an unconditional balance-integrity claim.

**35. Per-recipient provisioning is not serialized — static concurrency risk.** `provision_wallet` checks for a wallet, awaits faucet creation, then inserts it, without a recipient lock or conflict recovery. Distinct first transfers to the same recipient can both create external accounts before the unique `wallets.user_id` constraint rejects one insert. That protects the one-row invariant, but does not prove only one on-ledger account was provisioned, or that the losing job recovers cleanly. Existing duplicate-delivery tests claim the same transaction and therefore do not exercise this scenario. Sources: `xrpl_service.py:237`, `models/wallet.py:26`, `tests/test_queue.py`.

**36. Unknown settlement outcomes are displayed as definitive non-delivery.** A post-signing timeout can mean the payment succeeded, as the reconciliation code itself recognizes. Nevertheless, `frontend/templates/admin/transaction_detail.html` says “No UCTUSD reached the recipient” for every failed settlement, and the sender detail says the recipient balance was unchanged. The DB cache was not credited, but the ledger balance is unknown. Distinguish failed-and-confirmed from awaiting verification in both prose and display.

## Draft completeness and claims the repository cannot certify

Confirmed editorial defects in the attachment:

- §4 contains `ADD TABLE HERE`; Appendix A and Appendix B contain `DO AFTER`.
- §5.2 begins with an incomplete sentence and repeats the exchange-rate introduction.
- §6's heading and opening paragraph are duplicated.
- A second “7.2” after the appendices starts “Replace above table … maybe” and contradicts the main failure policy (finding 7).
- §9 still contains `[Team to decide]`, conflicting with the introduction/§13 framing that only fee calibration remains open.
- The opening starts with the fragment “UCTUSD, pre-funded …”. Several words are joined, such as `standardtier`, `42 sfor`, and `travel rulerequires`.
- The “four parameters” description and exact schema-column claims need the corrections above.

The cost table itself is supported by offline execution: USD **23.774775**, **49.874404**, **153.720191**, and **515.461050** for R500/R1,000/R3,000/R10,000 respectively. Removing the USD 1 floor gives approximately **9.25%** cost at R500, consistent with “about 9.3%.” Two minor refinements: R25 + USD 1 at R18.50/USD is **8.7%** of R500 (approximately 9%); the percentage cash-out fee reaches USD 1 around a **R1,941.12** send, so “roughly R1,900” is approximate. “Effective rate always higher” should be “higher with a positive margin”: the admin editor permits a zero margin, yielding equality.

Unverified from the reviewed files:

- Current treasury balance, issuer flags, faucet availability, network behavior, and whether the configured addresses currently match the attachment. `.env.example` matches its public issuer, treasury and currency values; that is configuration evidence, not an independent ledger check.
- The historical smoke-run results and 39-second end-to-end run. The scripts demonstrate the operations they would perform, but no retained smoke transcript/hash set was read. In particular, the smoke script's insufficient-balance case is a **recipient-to-issuer burn exceeding the recipient's balance**, not an exhausted-treasury payment. Do not cite that script alone as direct evidence of the latter scenario.
- The real timing JSON supports the reported summarized timings, but contains no transaction hashes, run timestamp, or individual raw samples. Its generator records duration before checking payment success and does not persist result codes. It is not independent proof of three successful validated payments.
- The 232 ms standalone bcrypt timing, hardware configuration, historical runtime versions, actual PostgreSQL 16 deployment, and zero data loss. These are statements in the performance report, not independently established by the retained counters/code.
- Statements that secrets were **never** committed or logged. The current `.env` is ignored and untracked; selected fake-ledger log tests exist. Neither proves every historical commit, deployed log, or exception path is secret-free.
- Business/corridor cost comparisons, regulatory assertions, licensing conclusions, and convenor instructions. These cannot be established by reading this codebase. No legal or market-source verification was performed, and the original project brief was not supplied as a separate source. The report therefore makes no independent judgment on their correctness or on compliance with an unseen brief.

## Section-level disposition

| Attachment section | Audit result |
|---|---|
| Metadata / asset identifiers | Public identifiers match `.env.example`; live state unverified. |
| 1. Business problem | Code supports a Testnet settlement mechanism; market/comparative claims require external evidence. Cost calculations checked. |
| 2. Scope / custody | Main architecture matches; qualify successful versus failed transfers, operational scripts, and changing existing assets. |
| 3. Journey | Normal path matches; correct rate, gate, receiver-flag and reserve details. |
| 4. Requirements | Counts correct; table missing; complete-implementation assertion needs criterion-level exceptions. |
| 5. Commercial model | Worked calculations/default seeds match; correct snapshot timing, rounding, precision and parameter count. |
| 6. Architecture | Stack matches; correct absolute signing/key-isolation, latency and scaling statements. |
| 7. Flows / failures | Ordinary state machines match; retry, finality and recovery corrections are material. |
| 8. Database | Eight domain tables and migrations 0001–0007 match; correct balance semantics, snapshot extent, KYC rule and SQL types/names. |
| 9. API | Listed business route families exist; JSON versus forms is accurate; status-code convention and pending decision need revision. |
| 10. Security | bcrypt, Fernet, centralized admin gates and object scoping present; narrow DB-compromise, session and historical guarantees. |
| 11. Regulatory considerations | Prototype controls inspected; legal conclusions unverified. |
| 12. Performance/testing | Test counts and most measurements supported; correct request count, latency exceptions, load profile, throughput interpretations and evidence claims. |
| 13. Assumptions/limitations | Many accurately describe the implementation; add the uncovered recovery/quote limitations and distinguish assumptions from guarantees. |
| Appendices / trailing material | Incomplete, with contradictory duplicate failure-policy text. |

The report's findings are based on the cited source paths and offline checks at the recorded commit. Application behavior under live infrastructure, concurrent schedules and historical runs is not certified where explicitly marked unverified or static risk.
