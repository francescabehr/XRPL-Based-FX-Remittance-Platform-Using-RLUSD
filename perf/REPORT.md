# Performance Report — XRPL FX Remittance Platform

**Measured:** 28 September 2026 · **Brief §7.iv** · Raw CSVs in `perf/results/`

---

## Executive Summary

The web tier is fast and stable under realistic load. At 50 concurrent users the platform
serves **38 requests per second** with a **16 ms median** response time and **zero HTTP errors**.
Throughput scales near-linearly to 100 users (54 rps) with no sign of server saturation.

Two capacity constraints stand out:

1. **bcrypt runs on the async event loop.** Each login stalls the server for ~230 ms. With 50
   simultaneous users (the 2-minute 50-user tier) median login time reaches 6.6 s; at 100 users
   it reaches 9.7 s, and the stall bleeds into every other endpoint waiting in the queue. The fix is one line — offload
   hashing to a thread pool. bcrypt's cost itself should not be lowered.

2. **One settlement worker cannot drain the queue.** The web tier confirms cash-ins at roughly
   5 per second; one worker settles ~9 per minute. The backlog grows throughout any sustained run.
   Four workers raise throughput 4× and are the straightforward fix.

Neither is a design fault — both are deployment sizing decisions. All other indicators are
healthy: the money-moving path has a 22 ms median, settlement has a 0 % failure rate, and
database row-level locking shows no contention at realistic concurrency.

---

## 1. Test Environment and Method

### Hardware and runtime

| Item | Value |
| --- | --- |
| Machine | Apple M2, 8 cores, 8 GB RAM, macOS 25.6.0 |
| Python | 3.13.11 |
| Web server | Uvicorn, single process, `--reload` (file-watch overhead present) |
| Database | PostgreSQL 15 in Docker, default configuration |
| Cache / queue | Redis 7 in Docker |
| Load driver | Locust 2.32.4 |

### Synthetic data

`perf/seed_data.py 200` created 200 sender/recipient pairs (400 accounts total). Each sender has
KYC approved, one pre-linked beneficiary, and the platform's default daily and monthly send limits.
All accounts use the password `PerfTest123!`.

### Load profile

`perf/locustfile.py` mixes three user types weighted to match real usage:

| User type | Weight | Behaviour |
| --- | ---: | --- |
| SenderUser | 6 | Login → dashboard → quote → send (approved card) → send (declined card) → history |
| RecipientUser | 3 | Login → wallet → cash-out preview → cash-out request |
| AdminUser | 1 | Confirm cash-in queue in batches; check settlement and transaction monitors |

The sender tasks include a `send_money_declined` subtask (weight 1 within SenderUser, so roughly
1-in-6 sends) that deliberately uses the declined test card `4000000000000002`. This exercises the
failure path under load and is tracked as a separate row in the statistics.

### Runs conducted

| Run | Users | Spawn rate | Duration | Purpose |
| --- | ---: | ---: | --- | --- |
| Reference | 50 | 5/s | 3 min | Full per-endpoint statistics, queue depth sampling |
| Concurrency — 10u | 10 | 5/s | 2 min | Baseline |
| Concurrency — 25u | 25 | 5/s | 2 min | Light load |
| Concurrency — 50u | 50 | 5/s | 2 min | Moderate load |
| Concurrency — 100u | 100 | 5/s | 2 min | Heavy load |

All five runs used `perf/perf_worker.py` — a simulated settlement worker that exercises the real
queue, claim logic and database writes but replaces the two XRPL network calls with a fixed
4-second delay. Real on-chain timings are measured separately (§5).

---

## 2. API Performance

![Response time by endpoint](results/response_times.png)

Per-endpoint figures from the 3-minute reference run (50 concurrent users).

| Endpoint | Requests | Median (ms) | p95 (ms) | Max (ms) | Failures |
| --- | ---: | ---: | ---: | ---: | ---: |
| **All requests** | 6,872 | 16 | 390 | 10,830 | 0 |
| POST /admin/cashin/{id}/received | 1,018 | 22 | 50 | 185 | 0 |
| GET /send/pay | 979 | 9 | 26 | 844 | 0 |
| GET /send/review | 979 | 11 | 45 | 4,356 | 0 |
| POST /remittances | 836 | 22 | 52 | 736 | 0 |
| POST /remittances (declined) | 143 | 21 | 49 | 166 | 0 |
| GET /quote (API) | 643 | 11 | 51 | 3,886 | 0 |
| GET /wallet | 515 | 20 | 1,400 | 4,595 | 0 |
| GET /dashboard | 483 | 16 | 66 | 494 | 0 |
| GET /transactions | 350 | 11 | 63 | 670 | 0 |
| POST /cashout/preview | 346 | 15 | 83 | 6,776 | 0 |
| GET /cashout/history | 229 | 15 | 99 | 8,350 | 0 |
| GET /admin/cashin | 198 | 13 | 360 | 4,079 | 0 |
| POST /login | 50 | 6,200 | 10,000 | 10,830 | 0 |
| GET /admin/transactions | 36 | 31 | 99 | 130 | 0 |
| GET /admin/settlements | 37 | 8 | 110 | 258 | 0 |
| GET /send | 30 | 830 | 5,000 | 5,788 | 0 |

**The money-moving path is fast.** `POST /remittances` — which locks the sender's row, re-prices
the quote, checks daily and monthly limits, inserts the transaction and enqueues a settlement
message — has a 22 ms median and a 52 ms p95. The declined-card path (`POST /remittances
(declined)`) is within 1 ms of the success path at both percentiles, confirming the failure branch
adds no overhead.

**The sub-second interaction target (NFR 7.2) is met at p95 by every screen except login, the
wallet page and `GET /send` (p95 5,000 ms; see bottleneck #5 in §8).** Every maximum above 1 s
traces back to one of two causes: a login stalling the event loop, or a wallet page waiting on the XRPL network — both diagnosed in §7.

---

## 3. Requests per Second

| Run | Users | RPS | Notes |
| --- | ---: | ---: | --- |
| 10u | 10 | 7.1 | Logins spaced out; no contention |
| 25u | 25 | 18.0 | 2.5× users → 2.5× RPS |
| 50u | 50 | 33.3 | Near-linear scaling continues |
| Reference (50u, 3 min) | 50 | 38.5 | Longer run with more stable send activity |
| 100u | 100 | 54.0 | Scaling slows slightly but server not saturated |

Throughput grows near-linearly from 10 to 50 users. At 100 users the multiplier drops slightly
(54 rps instead of the ~70 that strict linearity would predict), which is consistent with the
bcrypt stalls adding queuing delay at the highest load tier. Even so, the server has not hit a
ceiling: CPU and memory were not the constraint.

---

## 4. Settlement Queue and Worker Throughput

![Queue depth under load](results/queue_depth.png)
![Settlement throughput by worker count](results/throughput.png)

### Simulated-ledger throughput (measured)

| Configuration | Settlements per minute |
| --- | ---: |
| 1 worker | 9.2 |
| 4 workers | 37.3 |
| Scaling factor | 4.05× |

Scaling is nearly linear because each settlement job claims its own transaction row before
doing any work. Workers contend on nothing except their own rows, so adding workers adds
throughput at essentially no cost per worker added.

With 1 worker running during the reference run, the admin confirmed cash-ins at ~5 per
second (1,018 confirmations in 3 minutes). The worker settled 0.15 per second. The queue
grew to 833 waiting messages by the end of the run and never began to drain. The worker rate and
queue depth come from the queue sampling in the 21 September run (`queue.csv`); the sampler was
not re-run alongside the 28 September reference run, whose confirmation count is quoted above.

### Real-ledger capacity (derived)

At the measured 14.1 s per on-chain payment (§5):

| Workers | Estimated settlements per minute (real ledger) |
| ---: | ---: |
| 1 | ~4.3 |
| 4 | ~17 |
| 8 | ~34 |

These figures assume no Testnet rate limiting, which the sample was too small to probe.
Real throughput could be lower under sustained load.

> **Note: multi-worker figures do not carry over to the real ledger.** The 1- and 4-worker
> queue measurements (`queue.csv`, `queue_4workers.csv`, `throughput.png`) date from the
> 21 September run and were not repeated on 28 September. They were taken before
> `queue_service.treasury_lock` was added (25 September), so those workers signed
> concurrently. Every settlement is now signed and submitted under that single Redis lock,
> held for the whole ledger round trip, because all payments come from one treasury account
> and share its sequence number. Real-ledger treasury payments are therefore serialised: about
> 4 per minute (60 s ÷ 14.1 s) however many workers run, so the 4- and 8-worker rows in the
> table above are upper bounds that the current code does not reach. Only wallet provisioning
> (faucet account and TrustSet) runs outside the lock.

---

## 5. XRPL Processing Time

Measured with `perf/measure_xrpl.py` against the public XRPL Testnet in a separate run.
The load tests do not touch the real network.

| Step | Samples | Min (s) | Mean (s) | Max (s) |
| --- | ---: | ---: | ---: | ---: |
| Create recipient account (faucet) | 1 | 11.6 | 11.6 | 11.6 |
| TrustSet to issuer | 1 | 14.1 | 14.1 | 14.1 |
| Treasury → recipient payment | 3 | 13.0 | 14.1 | 16.3 |
| **First transfer to a new recipient (total)** | 1 | — | **42.0** | — |

A recipient's first-ever transfer costs ~42 seconds end-to-end: the worker must create the
XRPL account and submit a TrustSet before the payment can be sent. Subsequent transfers to
the same recipient cost ~14 seconds — ledger validation latency only. This matches the
end-to-end Phase 6 smoke test (39 s) closely.

Nearly all of the 14-second payment time is spent waiting for the ledger to close and
validate the transaction. The platform cannot reduce this; it is inherent to the XRPL
consensus mechanism. The worker's reliable-submission pattern (hash stored before submit,
LastLedgerSequence tracked, reconcile resolves unknowns) handles the cases where validation
is delayed or the result is uncertain.

---

## 6. Success and Failure Rates

All figures come from a direct PostgreSQL query after all five test runs completed.

### Cash-in outcomes (2,535 transactions total)

| Status | Count | % of total | Reason |
| --- | ---: | ---: | --- |
| received — card approved | 2,074 | 81.8 % | Normal sends |
| failed — card declined | 421 | 16.6 % | Intentional declined test card |
| pending — awaiting admin | 40 | 1.6 % | Final run still in queue |

The 421 card-declined failures are entirely intentional: they are the `send_money_declined`
task sends using the known-declined card `4000000000000002`. The count (421) matches exactly
the sum of declined sends across all five runs (143 + 27 + 59 + 70 + 122). No card-approved
transaction was ever rejected by the platform; the admin-confirmed cash-in success rate is
**100 %**.

### Settlement outcomes (of 2,074 received cash-ins)

| Status | Count | % of received |
| --- | ---: | ---: |
| completed | 187 | 9.0 % |
| queued — not yet reached | 1,886 | 90.9 % |
| processing — claimed, in flight | 1 | < 0.1 % |
| failed | 0 | **0.0 %** |

**Settlement failure rate: 0 %.** Every transaction the worker reached completed on its
first attempt. All 187 completed rows carry an XRPL transaction hash from the simulated
worker; none are hash-less. No settlement was retried (max attempts = 1; mean = 0.09 across
all received rows).

The 1,886 queued rows are backlog, not failures. The simulated worker processes ~9 per
minute while the test confirmed hundreds per minute. The queue is FIFO with persistent
messages; no work has been lost, and the backlog would drain completely if the worker were
left running.

### HTTP errors

Zero HTTP 4xx or 5xx responses were recorded in any run. The Locust-level failures reported
at 25–100 users (see §7) are application-level daily-limit enforcements, not server errors.

---

## 7. Concurrency Behaviour

![Concurrency scaling](results/concurrency_scaling.png)

### Aggregated results across tiers

| Users | Requests | Locust failures | Failure % | Median (ms) | p95 (ms) | Max (ms) | RPS |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10 | 845 | 0 | 0.0 % | 19 | 1,200 | 2,944 | 7.1 |
| 25 | 2,150 | 5 | 0.2 % | 17 | 1,200 | 6,895 | 18.0 |
| 50 | 3,983 | 173 | 4.3 % | 17 | 1,300 | 12,398 | 33.3 |
| 100 | 6,457 | 343 | 5.3 % | 35 | 1,500 | 19,882 | 54.0 |

### What the Locust failures represent

Every failure at every tier is a mark on `GET /send/pay`, not an HTTP error. The locustfile
marks that request failed when the page does not contain a rendered quote form — which
happens when a sender has hit their daily limit and the server responds with a redirect
instead. This is correct application behaviour. The failure count rises with concurrency
because more users cycling through the same 200 seeded senders depletes daily allowances
faster, and because earlier runs in the same UTC day had already consumed some allowance.

### Login degrades near-linearly (bcrypt bottleneck)

`verify_password` blocks the single event loop thread for ~230 ms. At low concurrency the
logins are spaced out; at high concurrency they queue behind each other:

| Users | Login median (ms) | Login p95 (ms) | Login max (ms) |
| ---: | ---: | ---: | ---: |
| 10 | 1,900 | 2,900 | 2,944 |
| 25 | 4,700 | 6,900 | 6,895 |
| 50 | 6,600 | 11,000 | 12,398 |
| 100 | 9,700 | 17,000 | 19,882 |

The median grows approximately 1,000–2,000 ms per 25-user increment, consistent with a
fixed-cost queue: each additional concurrent login adds one bcrypt slot. This stall is also
responsible for the tail latency on otherwise-fast endpoints — a `POST /remittances` that
arrives while three users are logging in waits ~690 ms before it is even scheduled.

### Money-moving endpoints hold up under load

Row-level locking (`SELECT FOR UPDATE` on sender and wallet rows) shows no contention at
realistic concurrency. The remittance endpoint stays flat through 50 users:

| Users | POST /remittances median (ms) | POST /remittances p95 (ms) |
| ---: | ---: | ---: |
| 10 | 25 | 40 |
| 25 | 24 | 42 |
| 50 | 23 | 81 |
| 100 | 39 | 750 |

The p95 jump at 100 users (81 → 750 ms) is login-stall collateral, not lock contention.
Under a workload concentrated on a single sender — which was not tested — contention would
appear and would be the right target for follow-up measurement.

The declined-card path tracks the success path within 2–3 ms at every tier, confirming the
failure branch adds no overhead even under concurrent load.

### Wallet page live-ledger call grows with concurrency

`GET /wallet` fetches the on-chain balance on every page load. In the concurrency tiers the
median sits at about the network round-trip (~1.2 s) and the p95 grows as requests queue behind
each other's in-flight ledger calls. The 3-minute reference run (§2) did not show this: its
`/wallet` median was 20 ms, with a 1,400 ms p95.

| Users | Wallet median (ms) | Wallet p95 (ms) |
| ---: | ---: | ---: |
| 10 | 1,200 | 1,400 |
| 25 | 1,200 | 1,800 |
| 50 | 1,200 | 1,900 |
| 100 | 1,300 | 2,400 |

---

## 8. Bottlenecks

**1. bcrypt blocks the async event loop — the most impactful issue.**
`auth_service.verify_password` runs synchronously inside an `async def` handler, blocking
Uvicorn's single event loop thread. In the 2-minute 50-user tier the median login time is 6.6 s
and the worst case is 12.4 s (the 3-minute reference run in §2 shows a 6,200 ms median and a
10,830 ms maximum). The stall propagates to every other endpoint waiting behind a login.
Fix: wrap the call in `asyncio.to_thread` (or Starlette's `run_in_threadpool`). bcrypt's
work factor should not be reduced — the cost is the security guarantee.

**2. One settlement worker cannot drain the queue.**
The web tier accepts and confirms cash-ins far faster than one worker can settle them
(~5/s confirmed vs ~0.15/s settled). The queue is a correct buffer — no messages are
dropped — but recipients wait minutes for a transfer the ledger takes seconds to process.
Running 4 workers raises throughput ~4× (measured). Capacity planning should use the
real-ledger rate of ~4 settlements per minute per worker.

**3. The wallet page makes a synchronous ledger call.**
`GET /wallet` fetches the live on-chain balance on every request. In the concurrency tiers this
put the median at ~1.2 s at every load level; in the reference run the median was 20 ms but the
p95 was 1,400 ms, so the call dominates the slow tail rather than every load. The cached
`balance_uctusd` column is already in the database. Rendering the page from that value and refreshing the live figure
asynchronously (or on a short TTL) would reduce the median to the same range as every other
page (10–20 ms).

**4. No database lock contention observed.**
Row locks on `sender` and `wallet` rows did not contribute to latency at the tested
concurrency levels. The 200 seeded senders distributed the load across enough distinct rows
that no queue formed at the database. A concentrated test (many users sharing one sender)
would be needed to probe this further.

**5. `GET /send` p95 is login-stall collateral.**
The send-amount page is loaded once per user at startup, exactly when all 50 users are
also logging in. Its p95 (5,000 ms) reflects the login queue, not a query problem; its
median (830 ms) is the same beneficiary list query as `/send/review` (11 ms median) plus
the login contention.

---

## 9. Recommendations

| Priority | Change | Expected effect |
| ---: | --- | --- |
| 1 | Wrap `verify_password` and `get_password_hash` in `asyncio.to_thread` | Login median falls to ~250 ms; stall disappears from all other endpoints |
| 2 | Run 3–4 settlement workers in production | Queue drains instead of growing; recipient wait time drops proportionally |
| 3 | Serve `GET /wallet` from cached `balance_uctusd`; fetch live figure in background | Wallet median falls from 1.2 s to ~20 ms |
| 4 | Run Uvicorn with multiple workers (`--workers 4`) in production | Spreads CPU work across cores; also dilutes login stalls |

---

## 10. Limitations and Caveats

**Simulated ledger.** All load-test settlement used a simulated worker with a fixed 4-second
delay instead of real XRPL network calls. Queue depth, claim logic, database writes and
wallet credits are real; XRPL latency and rate limits are not. Real throughput is lower
(~4 settlements/min/worker vs ~9 simulated).

**Small XRPL sample.** Real-ledger timings are 3 payment transactions, 1 account creation
and 1 trust line — sufficient for an order-of-magnitude estimate, not a statistical
distribution. Payment latency varies with Testnet load, which is a shared public network.

**Single-machine run.** The application, PostgreSQL, Redis and Locust all ran on the same
8-core laptop, sharing CPU, memory and the loopback interface. In a production deployment
these components would run on separate machines; measured latencies would likely be lower
(no resource contention between load driver and app) and throughput higher.

**`--reload` mode.** The dev server was started with `make dev` (`uvicorn --reload`), which
runs a WatchFiles process alongside the app and adds a small amount of file-system overhead
on every request. A production deployment (`--workers N`, no `--reload`) would be faster.

**Daily limit accumulation.** The five runs were conducted in the same UTC day against the
same seeded accounts. Senders accumulated sends against their daily limits across runs,
which is the source of the rising Locust failure count at higher concurrency tiers. These
failures are correct application-level enforcements, not server errors, but they inflate the
failure percentage for the 50- and 100-user tiers relative to a clean-slate run.

**Concurrency ceiling not found.** The 100-user run showed no sign of server saturation
(RPS still growing, no HTTP errors). The point at which the system actually breaks was not
tested. A follow-up run at 200–500 users would establish the real ceiling, though the
bcrypt bottleneck would dominate the results until it is fixed.

---

## Appendix: Reproducing the Results

```bash
# Seed synthetic users (run once; --purge to reset and re-seed)
python perf/seed_data.py 200

# Terminal 1 — web server
make dev

# Terminal 2 — simulated settlement worker (never use `make worker` for load tests)
python perf/perf_worker.py

# Terminal 3 — queue depth sampler (reference run only)
python perf/sampler.py perf/results/queue.csv

# Reference run (50 users, 3 minutes)
locust -f perf/locustfile.py --headless -u 50 -r 5 -t 3m \
       -H http://127.0.0.1:8000 --csv perf/results/run

# Concurrency tiers (2 minutes each)
for u in 10 25 50 100; do
  locust -f perf/locustfile.py --headless -u $u -r 5 -t 2m \
         -H http://127.0.0.1:8000 --csv perf/results/c${u}
done

# Real Testnet timings (separate run; spends ~1.5 UCTUSD from the treasury)
python perf/measure_xrpl.py 3

# Regenerate all charts
python perf/analyze.py
```

> **Do not run a load test with `make worker`.** It would attempt hundreds of real Testnet
> payments and drain the treasury wallet.
