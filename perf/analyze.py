"""
Phase 9 step 4: turn one `make perf-all` session into charts and summary.md.

Reads only what perf/run_all.py wrote in perf/results/:
    run_meta.json                 run id, environment, parameters, per-sub-run records
    run_stats.csv, run_failures.csv          reference load run (Locust)
    c<N>_stats.csv, c<N>_failures.csv        concurrency tiers (Locust)
    queue_<sub-run>.csv           queue + settlement sampler, one per sub-run
    <sub-run>_outcomes.json       DB cash-in / settlement / cash-out counts, end of sub-run
    xrpl_timings.json             real Testnet timings (this run, or carried over; see meta)

Writes perf/results/summary.md (every table perf/REPORT.md uses, run metadata at
the top) and the charts perf/results/*.png.

Usage: python perf/analyze.py
"""

import csv
import json
import os
from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS = Path(os.environ.get("PERF_RESULTS_DIR", "perf/results"))
CONFIRM = "POST /admin/cashin/{id}/received"
SPOTLIGHT = ("POST /login", "POST /remittances", "POST /remittances (declined)", "GET /wallet", "GET /send")

# Validated categorical palette (dataviz reference instance, light surface).
BLUE, ORANGE = "#2a78d6", "#eb6834"
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#898781", "#e1e0d9"

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": "#c3c2b7", "axes.labelcolor": INK, "text.color": INK,
    "xtick.color": MUTED, "ytick.color": MUTED, "grid.color": GRID,
    "font.size": 10, "axes.titlesize": 12, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False, "figure.dpi": 140,
})


def _read(name):
    path = RESULTS / name
    if not path.exists():
        return []
    with path.open() as handle:
        return list(csv.DictReader(handle))


def _json(name):
    path = RESULTS / name
    return json.loads(path.read_text()) if path.exists() else None


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _row(rows, name):
    return next((r for r in rows if r["Name"] == name), None)


def _style(ax, title, xlabel=""):
    ax.set_title(title, loc="left", pad=12)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED)
    ax.grid(axis="x" if ax.get_yticklabels() else "y", lw=0.8, alpha=0.9)
    ax.set_axisbelow(True)


# --- measures -------------------------------------------------------------------

def under_load(label: str, stats) -> dict:
    """Settlement progress while a load run was running (sampler + Locust)."""
    rows = _read(f"queue_{label}.csv")
    if len(rows) < 2:
        return {}
    span = _num(rows[-1]["t"]) - _num(rows[0]["t"])
    done = int(rows[-1]["completed"]) - int(rows[0]["completed"])
    confirm = _row(stats, CONFIRM)
    return {
        "span_s": span, "settled": done, "per_min": done / span * 60 if span else 0.0,
        "confirmations": int(confirm["Request Count"]) if confirm else 0,
        "depth_max": max(int(r["queue_depth"]) for r in rows), "depth_end": int(rows[-1]["queue_depth"]),
        "failed_end": int(rows[-1]["failed"]),
        # Cross-check: every confirmation Locust recorded should be a received row.
        "received_db": sum(r["count"] for r in (_json(f"{label}_outcomes.json") or {}).get(
            "cashin_by_settlement", []) if r["cashin"] == "received"),
    }


def drain(label: str, record: dict) -> dict:
    """Backlog drain: time from all workers registered to every row settled or failed."""
    seconds = record["drained_epoch"] - record["workers_ready_epoch"]
    outcome = _json(f"{label}_outcomes.json") or {}
    completed = sum(r["count"] for r in outcome.get("cashin_by_settlement", []) if r["settlement"] == "completed")
    return {
        "workers": record["workers"], "backlog": record["backlog"], "drain_s": seconds,
        "completed": completed, "per_min": record["settled_or_failed"] / seconds * 60 if seconds else 0.0,
        "timed_out": record.get("timed_out", False),
    }


# --- charts ---------------------------------------------------------------------

def chart_response_times(rows, users, when):
    """Median and p95 per endpoint as a dot plot on a log axis (login dwarfs the rest)."""
    endpoints = [r for r in rows if r["Name"] != "Aggregated" and int(r["Request Count"]) >= 20]
    if not endpoints:
        return
    endpoints.sort(key=lambda r: _num(r["95%"]))
    names = [r["Name"].replace("GET ", "").replace("POST ", "") for r in endpoints]
    p50 = [max(_num(r["50%"]), 1) for r in endpoints]
    p95 = [max(_num(r["95%"]), 1) for r in endpoints]

    fig, ax = plt.subplots(figsize=(8, 0.34 * len(names) + 1.8))
    for i, (a, b) in enumerate(zip(p50, p95)):
        ax.plot([a, b], [i, i], color=GRID, lw=2, zorder=1, solid_capstyle="round")
        ax.scatter([a], [i], s=60, color=BLUE, zorder=2, label="median" if i == 0 else None)
        ax.scatter([b], [i], s=60, color=ORANGE, zorder=2, label="95th percentile" if i == 0 else None)
        ax.text(b * 1.18, i, f"{b:,.0f}", va="center", fontsize=8, color=MUTED)
    ax.set_xscale("log")
    ax.set_xlim(min(p50) / 2, max(p95) * 3)
    ax.set_yticks(range(len(names)), names, fontsize=9)
    ax.set_ylim(-0.7, len(names) - 0.3)
    ax.legend(frameon=False, loc="lower right", fontsize=9)
    _style(ax, f"Response time by endpoint — {users} concurrent users ({when})", "milliseconds (log scale)")
    ax.grid(axis="x", lw=0.8, alpha=0.9)
    fig.tight_layout()
    fig.savefig(RESULTS / "response_times.png")
    plt.close(fig)


def chart_concurrency(users, tier_stats):
    """Median and p95 vs concurrency, overall and for the key endpoints."""
    def pick(rows, name, col):
        row = _row(rows, name)
        return max(_num(row[col]), 1) if row else None

    fig, (ax_top, ax_bot) = plt.subplots(2, 1, figsize=(8, 7), sharex=True, gridspec_kw={"hspace": 0.38})
    agg_med = [pick(s, "Aggregated", "50%") for s in tier_stats]
    agg_p95 = [pick(s, "Aggregated", "95%") for s in tier_stats]
    ax_top.plot(users, agg_med, marker="o", lw=2, color=BLUE, label="overall median")
    ax_top.plot(users, agg_p95, marker="o", lw=2, color=ORANGE, label="overall p95")
    for u, m, p in zip(users, agg_med, agg_p95):
        for v in (m, p):
            if v is not None:
                ax_top.annotate(f"{v:.0f}", (u, v), textcoords="offset points", xytext=(0, 8),
                                ha="center", fontsize=8, color=MUTED)
    ax_top.set_ylabel("milliseconds", color=MUTED)
    ax_top.legend(frameon=False, fontsize=9)
    _style(ax_top, "Overall latency vs concurrency")
    ax_top.grid(axis="y", lw=0.8, alpha=0.9)

    for name, col, colour, label in (
        ("POST /login", "50%", "#c0392b", "POST /login (median)"),
        ("GET /wallet", "95%", "#7b4dbd", "GET /wallet (p95)"),
        ("POST /remittances", "50%", "#2a9d5c", "POST /remittances (median)"),
    ):
        ax_bot.plot(users, [pick(s, name, col) for s in tier_stats], marker="o", lw=2, color=colour, label=label)
    ax_bot.set_yscale("log")
    ax_bot.set_ylabel("milliseconds (log scale)", color=MUTED)
    ax_bot.set_xlabel("concurrent users", color=MUTED)
    ax_bot.set_xticks(users, [str(u) for u in users])
    ax_bot.legend(frameon=False, fontsize=9)
    _style(ax_bot, "Key endpoint latency vs concurrency")
    ax_bot.grid(axis="y", lw=0.8, alpha=0.9)
    fig.tight_layout()
    fig.savefig(RESULTS / "concurrency_scaling.png")
    plt.close(fig)


def chart_queue(rows, users):
    """Queue depth against settlements completed during the reference run."""
    if len(rows) < 2:
        return
    t = [_num(r["t"]) for r in rows]
    depth = [int(r["queue_depth"]) for r in rows]
    done = [int(r["completed"]) - int(rows[0]["completed"]) for r in rows]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(t, depth, lw=2, color=ORANGE, label="messages waiting")
    ax.plot(t, done, lw=2, color=BLUE, label="settlements completed")
    ax.annotate(f"{depth[-1]:,} waiting", (t[-1], depth[-1]), textcoords="offset points",
                xytext=(-8, 8), ha="right", fontsize=9, color=MUTED)
    ax.annotate(f"{done[-1]} settled", (t[-1], done[-1]), textcoords="offset points",
                xytext=(-8, 10), ha="right", fontsize=9, color=MUTED)
    ax.legend(frameon=False, fontsize=9)
    _style(ax, f"Settlement queue during the reference run — {users} users, 1 worker", "seconds into the run")
    ax.set_ylabel("messages", color=MUTED)
    fig.tight_layout()
    fig.savefig(RESULTS / "queue_depth.png")
    plt.close(fig)


def chart_throughput(drains, xrpl):
    """Measured drain rate per worker count, plus the real-ledger treasury ceiling."""
    if not drains:
        return
    labels = [f"{d['workers']} worker{'s' if d['workers'] > 1 else ''}\n(simulated)" for d in drains]
    values = [d["per_min"] for d in drains]
    colors = [BLUE] * len(drains)
    if xrpl and "payment" in xrpl:
        labels.append("any worker count\n(real ledger, derived)")
        values.append(60 / xrpl["payment"]["mean_s"])
        colors.append(ORANGE)
    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(labels, values, color=colors, width=0.56)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.02,
                f"{value:.1f}", ha="center", fontsize=9, color=MUTED)
    ax.set_ylim(0, max(values) * 1.15)
    ax.set_ylabel("settlements per minute", color=MUTED)
    _style(ax, "Settlement throughput by worker count")
    if xrpl and "payment" in xrpl:
        fig.text(0.01, 0.01, f"Real-ledger bar: 60 s ÷ measured {xrpl['payment']['mean_s']} s payment; "
                             "treasury signing is serialised by treasury_lock.", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(RESULTS / "throughput.png")
    plt.close(fig)


# --- summary.md -----------------------------------------------------------------

def _ms(value) -> str:
    return f"{_num(value):,.0f}"


def _pct(part, whole) -> str:
    return f"{part / whole * 100:.1f} %" if whole else "—"


def summary(meta, stats, tiers, loads, drains, xrpl) -> str:
    env, params, runs = meta.get("environment", {}), meta["parameters"], meta["sub_runs"]
    out = [f"# Performance run {meta['run_id']}", ""]
    out += ["Generated by `perf/analyze.py` from this run's files only. Every table below names its sub-run.", ""]
    out += ["## Run metadata", "", "| Item | Value |", "| --- | --- |"]
    machine = env.get("machine", {})
    dirty = env.get("git_dirty_files", [])
    for key, value in (
        ("Run id", meta["run_id"]),
        ("Started / measurements finished (UTC)", f"{meta['started_at']} / {meta.get('measurements_finished_at', '?')}"),
        ("Status", meta.get("status")),
        ("Git commit", f"{env.get('git_commit', '?')[:12]} on {env.get('git_branch', '?')}"
                       + (f" ({len(dirty)} uncommitted files)" if dirty else " (clean)")),
        ("Machine", f"{machine.get('cpu')}, {machine.get('cpu_count')} cores, {machine.get('memory_gb')} GB, "
                    f"{machine.get('platform')}"),
        ("Python", env.get("python")),
        ("PostgreSQL", str(env.get("postgres", "")).split(" on ")[0]),
        ("Redis", env.get("redis")),
        ("Packages", ", ".join(f"{k} {v}" for k, v in env.get("packages", {}).items())),
        ("Web server", params["server"]),
        ("Worker", f"{params['worker']}, {params['xrpl_latency_s']} s simulated payment "
                   f"(+{params['xrpl_latency_s'] / 2} s simulated provisioning)"),
        ("Synthetic data", f"{params['pairs']} sender/recipient pairs, purged and re-seeded before every sub-run"),
        ("Database / Redis", f"{params['db_name']} (recreated) / {params['redis_url']} (flushed before every sub-run)"),
        ("Real XRPL timings", meta.get("xrpl_timings", {}).get("source")),
        ("Topology", env.get("same_machine")),
    ):
        out.append(f"| {key} | {value} |")
    out += ["", "### Sub-runs", "", "| Sub-run | Kind | Users / workers | Duration / backlog | Started (UTC) | Problems |",
            "| --- | --- | ---: | --- | --- | --- |"]
    for label, r in runs.items():
        size = r.get("users", r.get("workers"))
        length = r.get("duration") or f"{r.get('backlog')} settlements"
        out.append(f"| {label} | {r['kind']} | {size} | {length} | {r['started_at']} | "
                   f"{'; '.join(r.get('problems', [])) or 'none'} |")
    if meta.get("errors"):
        out += ["", "**Errors recorded:**", ""] + [f"- {e}" for e in meta["errors"]]

    # Reference run, per endpoint.
    ref = runs.get("run", {})
    out += ["", f"## API response times — reference run ({ref.get('users')} users, {ref.get('duration')})", "",
            "Source: `run_stats.csv`.", "",
            "| Endpoint | Requests | Failures | Median (ms) | p95 (ms) | p99 (ms) | Max (ms) | Req/s |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for row in sorted(stats, key=lambda r: (r["Name"] != "Aggregated", -int(r["Request Count"]))):
        name = "**All requests**" if row["Name"] == "Aggregated" else row["Name"]
        out.append(f"| {name} | {int(row['Request Count']):,} | {row['Failure Count']} | {_ms(row['50%'])} | "
                   f"{_ms(row['95%'])} | {_ms(row['99%'])} | {_ms(row['Max Response Time'])} | "
                   f"{_num(row['Requests/s']):.1f} |")

    # Tiers.
    out += ["", "## Concurrency tiers", "", "Source: `c<N>_stats.csv`, one fresh sub-run per tier "
            f"({params['tier_time']} each). The reference run is listed for comparison.", "",
            "| Sub-run | Users | Requests | Failures | Failure % | Median (ms) | p95 (ms) | Max (ms) | Req/s |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, users, rows in [("run", ref.get("users"), stats)] + tiers:
        agg = _row(rows, "Aggregated")
        if agg:
            n, f = int(agg["Request Count"]), int(agg["Failure Count"])
            out.append(f"| {label} | {users} | {n:,} | {f:,} | {_pct(f, n)} | {_ms(agg['50%'])} | "
                       f"{_ms(agg['95%'])} | {_ms(agg['Max Response Time'])} | {_num(agg['Requests/s']):.1f} |")
    out += ["", "### Key endpoints by sub-run", "",
            "| Sub-run | Users | Endpoint | Requests | Median (ms) | p95 (ms) | Max (ms) |",
            "| --- | ---: | --- | ---: | ---: | ---: | ---: |"]
    for label, users, rows in [("run", ref.get("users"), stats)] + tiers:
        for name in SPOTLIGHT:
            row = _row(rows, name)
            if row:
                out.append(f"| {label} | {users} | {name} | {int(row['Request Count']):,} | {_ms(row['50%'])} | "
                           f"{_ms(row['95%'])} | {_ms(row['Max Response Time'])} |")

    # Failures.
    out += ["", "## Locust failures by reason", "", "Source: `<sub-run>_failures.csv`.", "",
            "| Sub-run | Request | Reason | Occurrences |", "| --- | --- | --- | ---: |"]
    any_failure = False
    for label in ["run"] + [t[0] for t in tiers]:
        for row in _read(f"{label}_failures.csv"):
            any_failure = True
            out.append(f"| {label} | {row['Name']} | {row['Error']} | {row['Occurrences']} |")
    if not any_failure:
        out.append("| — | — | none recorded | 0 |")

    # Queue.
    out += ["", "## Settlement queue under load (1 simulated worker)", "",
            "Source: `queue_<sub-run>.csv` (sampled every second) and the confirmation count in `<sub-run>_stats.csv`.",
            "Confirmed (HTTP) is Locust's count of confirmation requests; received (DB) is the database count "
            "at the end of the sub-run. They should match.",
            "", "| Sub-run | Users | Sampled (s) | Confirmed (HTTP) | Received (DB) | Settled | Settled / min | Max depth | Depth at end |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for label, users, q in loads:
        if q:
            out.append(f"| {label} | {users} | {q['span_s']:.0f} | {q['confirmations']:,} | {q['received_db']:,} | {q['settled']} | "
                       f"{q['per_min']:.1f} | {q['depth_max']:,} | {q['depth_end']:,} |")
    out += ["", "## Worker scaling — same pre-filled backlog", "",
            "Source: `run_meta.json` (workers-ready and drained timestamps) and `workers<N>_outcomes.json`. "
            f"Each drain starts from {params['backlog']} confirmed cash-ins booked by `perf/backlog.py`, "
            "one per new recipient, after a purge and re-seed.", "",
            "| Workers | Backlog | Drain time (s) | Completed | Settlements / min | vs 1 worker |",
            "| ---: | ---: | ---: | ---: | ---: | ---: |"]
    base = drains[0]["per_min"] if drains else 0
    for d in drains:
        flag = " (timed out)" if d["timed_out"] else ""
        out.append(f"| {d['workers']} | {d['backlog']} | {d['drain_s']:.1f}{flag} | {d['completed']} | "
                   f"{d['per_min']:.1f} | {d['per_min'] / base:.2f}× |" if base else "")
    lat = params["xrpl_latency_s"]
    out += ["", f"Simulated timings: provisioning {lat / 2} s (outside `treasury_lock`), payment {lat} s "
            f"(inside it). Derived, not measured: one worker cannot beat 60 ÷ {lat * 1.5} = {60 / (lat * 1.5):.1f}/min; "
            f"any number of workers cannot beat 60 ÷ {lat} = {60 / lat:.1f}/min."]

    # XRPL.
    source = meta.get("xrpl_timings", {})
    out += ["", "## Real XRPL Testnet timings", "", f"Source: `xrpl_timings.json` — {source.get('source')}"
            + (f"; file last committed {source['file_last_commit']}" if source.get("file_last_commit") else "") + ".", ""]
    if xrpl:
        out += ["| Step | Samples | Min (s) | Mean (s) | Max (s) |", "| --- | ---: | ---: | ---: | ---: |"]
        for step, label in (("faucet_account", "Create recipient account (faucet)"),
                            ("trust_line", "TrustSet to issuer"), ("payment", "Treasury → recipient payment")):
            if step in xrpl:
                d = xrpl[step]
                out.append(f"| {label} | {d['samples']} | {d['min_s']} | {d['mean_s']} | {d['max_s']} |")
        out.append(f"| **First transfer to a new recipient** | 1 | — | {xrpl.get('first_transfer_total_s')} | — |")
        if "payment" in xrpl:
            out += ["", f"Derived real-ledger settlement ceiling (any worker count, `treasury_lock`): "
                        f"60 ÷ {xrpl['payment']['mean_s']} = {60 / xrpl['payment']['mean_s']:.1f} per minute."]
    else:
        out.append("No real-ledger timings available.")

    # DB outcomes.
    out += ["", "## Cash-in and settlement outcomes (database)", "",
            "Source: `<sub-run>_outcomes.json`, queried at the end of each sub-run, synthetic users only.", "",
            "| Sub-run | Cash-in | Settlement | Count | % of sub-run |", "| --- | --- | --- | ---: | ---: |"]
    for label in runs:
        outcome = _json(f"{label}_outcomes.json")
        if not outcome:
            continue
        total = sum(r["count"] for r in outcome["cashin_by_settlement"])
        for r in outcome["cashin_by_settlement"]:
            out.append(f"| {label} | {r['cashin']} | {r['settlement']} | {r['count']:,} | {_pct(r['count'], total)} |")
    out += ["", "| Sub-run | Completed without hash | Max settlement attempts | Mean attempts (received) | Cash-out requests |",
            "| --- | ---: | ---: | ---: | --- |"]
    for label in runs:
        outcome = _json(f"{label}_outcomes.json")
        if outcome:
            cashouts = ", ".join(f"{k} {v}" for k, v in outcome["cashout_by_status"].items()) or "none"
            out.append(f"| {label} | {outcome['completed_without_hash']} | {outcome['settlement_attempts_max']} | "
                       f"{outcome['settlement_attempts_mean']} | {cashouts} |")
    return "\n".join(out) + "\n"


def main() -> None:
    meta = _json("run_meta.json")
    if not meta:
        raise SystemExit("perf/results/run_meta.json not found — run `make perf-all` first.")
    runs = meta["sub_runs"]
    when = datetime.fromisoformat(meta["started_at"]).strftime("%-d %b %Y")
    stats = _read("run_stats.csv")
    tier_labels = [(label, r["users"]) for label, r in runs.items() if r["kind"] == "load" and label != "run"]
    tiers = [(label, users, _read(f"{label}_stats.csv")) for label, users in tier_labels]
    loads = [(label, r["users"], under_load(label, _read(f"{label}_stats.csv")))
             for label, r in runs.items() if r["kind"] == "load"]
    drains = [drain(label, r) for label, r in runs.items() if r["kind"] == "drain" and "drained_epoch" in r]
    xrpl = _json("xrpl_timings.json")

    chart_response_times(stats, runs.get("run", {}).get("users"), when)
    chart_queue(_read("queue_run.csv"), runs.get("run", {}).get("users"))
    chart_throughput(drains, xrpl)
    if tiers:
        chart_concurrency([u for _, u, _ in tiers], [rows for _, _, rows in tiers])

    (RESULTS / "summary.md").write_text(summary(meta, stats, tiers, loads, drains, xrpl))
    print(f"Wrote {RESULTS}/summary.md and charts {RESULTS}/*.png")


if __name__ == "__main__":
    main()
