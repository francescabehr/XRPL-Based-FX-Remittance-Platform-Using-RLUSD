"""
Phase 9 step 2: load profile for the platform (Brief §7.iv).

Three user types run together, matching how the system is actually used:

  SenderUser    (most users)  login, dashboard, quote, full send, history
  RecipientUser (some)        login, wallet, cash-out form and request
  AdminUser     (one)         works the cash-in queue, so settlements keep flowing

Accounts come from perf/seed_data.py. Run the app, a worker (perf/perf_worker.py
for load runs), and then:

    locust -f perf/locustfile.py --headless -u 50 -r 5 -t 3m \
           -H http://127.0.0.1:8000 --csv perf/results/run

Every task names its request explicitly, so a URL containing an id does not
scatter across hundreds of rows in the statistics.
"""

import os
import random
import re

from locust import HttpUser, between, events, task

PASSWORD = "PerfTest123!"
PAIRS = int(os.environ.get("PERF_PAIRS", "200"))
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "ChangeMe123!")

CARD = {"card_number": "4242 4242 4242 4242", "card_expiry": "12/30", "card_cvv": "123", "card_name": "Load Test"}
CASHIN_ROW = re.compile(r'/admin/cashin/([0-9a-f-]{36})/received')
RATE_FIELD = re.compile(r'name="exchange_rate" value="([\d.]+)"')
CASHOUT_FIELDS = re.compile(
    r'name="market_rate" value="([\d.]+)".*?name="cashout_fee_usd" value="([\d.]+)".*?name="net_payout" value="([\d.]+)"',
    re.S,
)

_next_index = {"sender": 0, "recipient": 0}


def _claim(kind: str) -> int:
    """Hand each spawned user its own seeded account, so they don't collide."""
    i = _next_index[kind] % PAIRS
    _next_index[kind] += 1
    return i


class _LoggedIn(HttpUser):
    abstract = True
    wait_time = between(1, 3)
    email = ""

    def on_start(self):
        with self.client.post(
            "/login", data={"email": self.email, "password": self.password},
            name="POST /login", catch_response=True,
        ) as response:
            if "/login" in response.url:
                response.failure("login rejected")

    @property
    def password(self):
        return PASSWORD


class SenderUser(_LoggedIn):
    """A sender: checks limits, prices a transfer, and pays for it."""

    weight = 6

    def on_start(self):
        self.email = f"perf.sender{_claim('sender')}@loadtest.local"
        super().on_start()
        self.beneficiary_id = None
        self._load_beneficiary()

    def _load_beneficiary(self):
        with self.client.get("/send", name="GET /send", catch_response=True) as response:
            match = re.search(r'<option value="([0-9a-f-]{36})"', response.text)
            if match:
                self.beneficiary_id = match.group(1)
            else:
                response.failure("no beneficiary on the send page")

    @task(3)
    def dashboard(self):
        self.client.get("/dashboard", name="GET /dashboard")

    @task(4)
    def quote(self):
        if not self.beneficiary_id:
            return
        amount = random.choice([100, 250, 500, 750])
        self.client.get(
            f"/quote?beneficiary_id={self.beneficiary_id}&zar_amount={amount}",
            name="GET /quote (API)",
        )

    @task(5)
    def send_money(self):
        """The full send: review the quote, open the pay page, submit the card."""
        if not self.beneficiary_id:
            return
        amount = random.choice([100, 150, 200, 250])
        query = f"beneficiary_id={self.beneficiary_id}&zar_amount={amount}"

        self.client.get(f"/send/review?{query}", name="GET /send/review")
        with self.client.get(f"/send/pay?{query}", name="GET /send/pay", catch_response=True) as response:
            rate = RATE_FIELD.search(response.text)
            if not rate:
                response.failure("pay page carried no quote")
                return

        form = {"beneficiary_id": self.beneficiary_id, "zar_amount": str(amount),
                "exchange_rate": rate.group(1), **CARD}
        with self.client.post("/remittances", data=form, name="POST /remittances", catch_response=True) as response:
            # A daily-limit refusal re-renders the pay page: expected under load,
            # not a server failure, but it must not be counted as a successful send.
            if "Daily limit exceeded" in response.text or "Monthly limit" in response.text:
                response.failure("limit reached (expected once a seeded sender is spent)")

    @task(2)
    def history(self):
        self.client.get("/transactions", name="GET /transactions")


class RecipientUser(_LoggedIn):
    """A recipient: checks their wallet and tries to cash out."""

    weight = 3

    def on_start(self):
        self.email = f"perf.recipient{_claim('recipient')}@loadtest.local"
        super().on_start()

    @task(5)
    def wallet(self):
        self.client.get("/wallet", name="GET /wallet")

    @task(2)
    def cashout_history(self):
        self.client.get("/cashout/history", name="GET /cashout/history")

    @task(3)
    def request_cashout(self):
        """Price a cash-out, then request it if the balance allows."""
        form = {"uctusd_amount": "1", "target_currency": random.choice(["USD", "ZAR"])}
        with self.client.post(
            "/cashout/preview", data=form, name="POST /cashout/preview", catch_response=True
        ) as response:
            if response.status_code == 400:
                response.success()  # no balance yet — a valid answer, not an error
                return
            priced = CASHOUT_FIELDS.search(response.text)
            if not priced:
                return

        market_rate, fee, net = priced.groups()
        self.client.post(
            "/cashout",
            data={**form, "market_rate": market_rate, "cashout_fee_usd": fee, "net_payout": net},
            name="POST /cashout",
        )


class AdminUser(_LoggedIn):
    """One admin working the cash-in queue, so settlement keeps being fed."""

    weight = 1
    wait_time = between(2, 4)

    def on_start(self):
        self.email = ADMIN_EMAIL
        super().on_start()

    @property
    def password(self):
        return ADMIN_PASSWORD

    @task(5)
    def confirm_cashins(self):
        with self.client.get("/admin/cashin", name="GET /admin/cashin", catch_response=True) as response:
            ids = CASHIN_ROW.findall(response.text)
        for txn_id in ids[:10]:
            self.client.post(f"/admin/cashin/{txn_id}/received", name="POST /admin/cashin/{id}/received")

    @task(1)
    def settlement_monitor(self):
        self.client.get("/admin/settlements", name="GET /admin/settlements")

    @task(1)
    def transaction_monitor(self):
        self.client.get("/admin/transactions", name="GET /admin/transactions")


@events.quitting.add_listener
def _summary(environment, **_):
    stats = environment.stats.total
    print(f"\nRequests: {stats.num_requests}  failures: {stats.num_failures}  "
          f"median: {stats.median_response_time} ms  p95: {stats.get_response_time_percentile(0.95)} ms  "
          f"rps: {stats.total_rps:.1f}")
