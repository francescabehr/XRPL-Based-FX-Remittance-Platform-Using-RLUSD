Work through this audit and fix each issue. Read the code and VERIFY before
changing — for anything touching XRPL result-code parsing or boundary amounts,
reproduce it by running the actual code/regex first (that's how the worst bug
below was found). Do the money-losing items (Tier 1) first. For each fix, keep
the existing conditional-UPDATE pattern the codebase already uses for state
transitions rather than inventing a new one.

═══ TIER 1 — money moves wrongly or is lost with no recovery path ═══

1. _result_code misreads tesSUCCESS → wrong cash-out restore
   xrpl_service.py:52,129-131 ; cashout_worker.py:286-300
   xrpl-py's reliable-submission timeout raises "...Prelim result: tesSUCCESS",
   and the regex extracts 'tesSUCCESS' (verified). The burn branch only holds on
   result_code=="submission_error", so this falls through to fail_and_restore and
   credits back UCTUSD for a burn that may have landed — violating the
   never-reverse-an-unknown-burn rule.
   Fix: don't derive outcome from a substring of the exception. Treat a
   reliable-submission timeout/"LastLedgerSequence passed" as UNKNOWN (hold, don't
   restore). Only classify as tec*/failed on an actual validated non-success.
   Also verify: do ter* codes (e.g. terQUEUED) reach the same path? If so they'd
   wrongly restore too — confirm against your xrpl-py version.

2. No minimum send amount → negative UCTUSD, ZAR taken anyway  [both]
   fx_service.py:167-173 ; cashin_service.py:107-159 ; transactions.py:108
   net_zar = zar_send - fee has no floor. R10 → uctusd_amount = -0.802862
   (verified); R25.38 → 0.000000. Row is created, admin confirms, worker calls
   uctusd(-x) → ValueError → caught as "transient" → 3 pointless retries → failed,
   with ZAR booked, allowance consumed, no refund path anywhere.
   Fix: raise in calculate_quote when net_zar<=0 or uctusd_amount<=0 (so the quote
   screen also refuses); ideally add configurable min_send_zar to FeeConfig checked
   in quote_for AND create_remittance. Make TypeError/ValueError from uctusd() a
   TERMINAL worker failure, not a retryable one.

3. Settlement retry treats elapsed time as proof a payment died → double payment [both]
   cashin_service.py:37-39,262-297 ; xrpl_service.py:194-202
   get_transaction_result returns None for both "never included" and "node can't
   see it". After LEDGER_EXPIRY_MARGIN the hash is wiped and re-sent; a validated
   payment whose response was lost gets paid a SECOND time. This is exactly the
   reasoning cashout reconcile forbids ("wall-clock is never evidence"), but
   transactions lacks the columns to do it right.
   Fix: mirror the cash-out design. Add settlement_last_ledger_sequence +
   settlement_submitted_ledger_index; switch send_from_treasury to on_signed_tx=
   (SignedTx/submitted_ledger_index already supported in submit()); gate resend on
   latest_ledger() > last_ledger AND has_complete_ledger_range(submitted,last).
   Delete LEDGER_EXPIRY_MARGIN. Note test_queue.py:237-256 encodes the current
   behaviour as intended — update it. Interim: refuse the resend whenever a hash is
   set and the ledger says None.

4. Cash-out stranded forever with balance debited  [both]
   cashout_worker.py:90-110 ; cashout_service.py:385-394,336-337
   Worker claims (burn_started_at set) then dies before signing; RQ moves the job
   to the failed registry, no message remains. sweep_unpublished skips it
   (burn_started_at IS NULL only); reconcile refuses (no hash); approve/reject need
   status=requested. Row sits approved+debited, nothing can move it. Settlement has
   an equivalent recovery; cash-out doesn't.
   Fix: widen sweep_unpublished to (burn_started_at IS NULL OR burn_started_at 
   now-STUCK_AFTER), keeping xrpl_burn_tx_hash IS NULL as the hard guard. Run the
   sweep periodically, not only at startup (main.py:18-30 sweeps once at boot).
   Verify the RQ-dead-job assumption against your RQ version (SimpleWorker moves to
   failed registry rather than requeueing).

5. Rate-drift guard silently bypassable  [both]
   transactions.py:231,256 ; cashin_service.py:126-127
   exchange_rate is optional; _parse_amount returns None on anything unparsable
   or <=0; None means "skip the check". Any POST omitting/mangling the field is
   priced at the current rate and charged — contradicting requirements.md §382.
   Fix: make exchange_rate required; treat absent/unparsable as a REFUSAL, not a
   skip. Consider also comparing transaction_fee/uctusd_amount so a fee-only config
   change is caught (as the cash-out side already does).

═══ TIER 2 — crashes, wrong state, correctness ═══

6. fail_settlement is the only unguarded transition
   settlement_worker.py:129-138
   Plain ORM assignment, no WHERE status IN (...). A late worker can drag a
   just-requeued row back to failed and overwrite xrpl_tx_hash.
   Fix: conditional UPDATE guarded on the expected status, like every sibling.

7. _publish's failure write is unconditional, can clobber a live worker
   cashin_service.py:162-171
   Sets settlement_status=failed via ORM assignment. If enqueue raises after Redis
   accepted the job, a worker may already have moved queued→processing; this stamps
   failed over it, then complete_settlement's conditional matches nothing — payment
   lands on-ledger, wallet never credited.
   Fix: conditional UPDATE ... WHERE settlement_status='queued'.

8. complete_settlement can die mid-transition
   settlement_worker.py:117-121
   .scalar_one() on the wallet; if no wallet row exists, NoResultFound propagates
   after the status UPDATE already ran, rolls back, and the row sits in processing
   after a possibly-successful payment.
   Fix: handle the missing-wallet case explicitly before/within the transition.

9. approve_kyc / reject_kyc have no state guard
   kyc_service.py:91-129
   Writes submission.status and user.kyc_status unconditionally — an admin can
   approve an already-rejected or stale submission, flipping the user to approved.
   Fix: guard on status==pending; raise if it doesn't match.

10. parse_amount lets InvalidOperation escape → 500 on /cashout/preview
    cashout_service.py:83 (quantize outside the try)
    parse_amount("1e40") raises (verified). POST /cashout catches ArithmeticError;
    POST /cashout/preview catches only CashOutError → unhandled 500.
    Fix: move the quantize inside the try / normalise to CashOutError.

11. Same Decimal overflow crashes POST /remittances
    transactions.py:240 ; create_remittance catches only RemittanceError
    calculate_quote raises InvalidOperation on 1e40 (verified). /quote and
    _quote_context handle it; this path doesn't.
    Fix: catch (FXConfigError, InvalidOperation)/ArithmeticError here too.

12. DivisionByZero not in the caught set
    transactions.py:63,128 catch (FXConfigError, InvalidOperation)
    DivisionByZero is a sibling of InvalidOperation, not a subclass; effective_rate
    of 0 (reachable via FX_RATE_SOURCE=static, FX_STATIC_MARKET_RATE=0) is a 500.
    Fix: catch ArithmeticError.

13. retry_settlement erases the prior attempt's hash  [both]
    cashin_service.py:286 sets xrpl_tx_hash=None
    Destroys the only record of a payment that may exist on-ledger — the exact
    thing needed if #3 fires. Keep it (previous_tx_hashes column or attempts table).

14. settlement_worker.claim lacks the xrpl_tx_hash IS NULL guard its twin has
    settlement_worker.py:69-83 vs cashout_worker.py:91-99
    Currently unreachable, but it's the one guard that makes a double-submit
    structurally impossible on the cash-out side, and costs one line. Add as
    defence in depth, especially alongside #3.

15. Concurrent settlement workers collide on the treasury sequence
    xrpl_service.py:275-288
    Every settlement signs from the one treasury account; autofill reads the
    sequence per call, so two concurrent workers autofill the same sequence — one
    validates, the other gets tefPAST_SEQ and is marked failed. Masked today by the
    single SimpleWorker, but the queue exists to scale.
    Fix: serialise treasury signing (Redis lock / single-concurrency queue), or
    document single-worker as a hard constraint.

16. Limit windows are UTC; every timestamp the user sees is UTC+02:00
    limit_service.py:80-91 vs templating.py:16-22 ; cashin_service.py:382-384
    A 00:30 SAST send shows as "today" but counts against yesterday's UTC daily
    allowance, resetting at 02:00 local. Admin date-range filter (_day_bounds builds
    UTC midnight from a SAST-picked date) silently misses the first two hours of
    each day.
    Fix: compute day/month boundaries in display_timezone, convert to UTC for the
    query; same in _day_bounds.

17. Abandoned pending cash-ins consume allowance permanently
    limit_service.py:70-76 counts everything except failed
    A sender who never pays burns daily+monthly allowance with no expiry. Excluding
    in-flight rows would reopen the TOCTOU hole, so use an expiry instead.
    Fix: auto-fail pending cash-ins older than N minutes (sweep, or a
    created_at > now-interval clause).

18. Admin seeding doesn't normalise the email
    seed_admin.py:15 stores admin_email verbatim; get_user_by_email lowercases
    Any uppercase in ADMIN_EMAIL yields an admin who can never log in and whose
    duplicate check never matches.
    Fix: .lower().strip() at seed time.

19. Rounding inconsistency / zero-amount in cash-out parsing
    cashout_service.py:83
    quantize() with no rounding arg → ROUND_HALF_EVEN, while fx_service is HALF_UP
    throughout. Also "0.0000004" passes >0 then quantises to 0 — only saved today by
    the fee floor; with cashout_fee_min_usd=0 it reaches uctusd() and raises.
    Fix: quantize with ROUND_HALF_UP; reject amounts that quantise to 0.

═══ TIER 3 — robustness, edge cases, spec ═══

20. cashout_worker RETRY_DELAYS[min(burn_attempts-1, ...)] indexes -1 (→60s) if
    burn_attempts were ever 0. cashout_worker.py:230. Unreachable today (claim
    increments first); guard the index anyway.

21. _pricing_matches returns True when accepted is None — any future caller of
    create_request that omits it silently loses the price-lock. cashout_service.py:
    148-155. Make the None case explicit or required.

22. Settlement has no equivalent to sweep_unpublished for the commit-before-publish
    window in mark_cashin_received. Recoverable after 10 min via /admin/settlements,
    but the two flows have different guarantees. cashin_service.py:204-205.

23. A faucet-funded account can be orphaned if db.commit() fails after
    generate_faucet_wallet — funded XRPL account exists, seed gone. Testnet-only.
    xrpl_service.py:251-267. Log the address (never the seed) before commit.

24. cashout_payout deviates from FR-CO-02 AS WRITTEN — not a code bug.
    fx_service.py:126-145 computes (uctusd−fee)×rate; FR-CO-02 says (uctusd×rate)−fee.
    The code's approach is the more sensible reading (one USD fee definition, payout
    matches quote-time estimate). Fix the SPEC: amend FR-CO-02 to state the fee is
    charged in USD before conversion, so spec and code agree. Also note: the $1
    cash-out fee is destroyed in the burn, not retained as tokens — invisible to any
    future platform-P&L modelling.

25. measure_xrpl.py:82-84 — IndexError if invoked with 0 payments. Guard the arg.

26. dependencies.py:41 — HTTPException(302, headers={"Location":...}) emits a JSON
    body; browsers follow it but an API client gets {"detail":"Found"}. Return a
    RedirectResponse instead.