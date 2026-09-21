"""
Phase 9 step 4: turn the measured runs into the report's tables and charts.

Reads what the runs produced:
    perf/results/run_stats.csv        Locust per-endpoint statistics
    perf/results/queue.csv            queue + settlement sampler, 1 worker, under load
    perf/results/queue_4workers.csv   the same while 4 workers drained the backlog
    perf/results/xrpl_timings.json    real Testnet timings

Writes charts to perf/results/*.png and prints the markdown tables used in
perf/REPORT.md.

Usage: python perf/analyze.py
"""

import csv
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

RESULTS = Path("perf/results")

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
    with (RESULTS / name).open() as handle:
        return list(csv.DictReader(handle))


def _style(ax, title, xlabel=""):
    ax.set_title(title, loc="left", pad=12)
    if xlabel:
        ax.set_xlabel(xlabel, color=MUTED)
    ax.grid(axis="x" if ax.get_yticklabels() else "y", lw=0.8, alpha=0.9)
    ax.set_axisbelow(True)


def chart_response_times(rows):
    """Median and p95 per endpoint as a dot plot.

    Login is ~500x slower than the fastest endpoint, so a linear bar chart hides
    everything else. Dots on a log axis keep both ends readable (bars would not:
    a bar's length is meaningless on a log scale).
    """
    endpoints = [r for r in rows if r["Name"] != "Aggregated" and int(r["Request Count"]) >= 20]
    endpoints.sort(key=lambda r: float(r["95%"]))
    names = [r["Name"].replace("GET ", "").replace("POST ", "") for r in endpoints]
    p50 = [max(float(r["50%"]), 1) for r in endpoints]
    p95 = [max(float(r["95%"]), 1) for r in endpoints]

    fig, ax = plt.subplots(figsize=(8, 0.34 * len(names) + 1.8))
    for i, (a, b) in enumerate(zip(p50, p95)):
        ax.plot([a, b], [i, i], color=GRID, lw=2, zorder=1, solid_capstyle="round")
        ax.scatter([a], [i], s=60, color=BLUE, zorder=2, label="median" if i == 0 else None)
        ax.scatter([b], [i], s=60, color=ORANGE, zorder=2, label="95th percentile" if i == 0 else None)
        ax.text(b * 1.18, i, f"{b:,.0f}", va="center", fontsize=8, color=MUTED)
    ax.set_xscale("log")
    ax.set_xlim(4, max(p95) * 3)
    ax.set_xticks([10, 100, 1000, 10000], ["10", "100", "1,000", "10,000"])
    ax.set_yticks(range(len(names)), names, fontsize=9)
    ax.set_ylim(-0.7, len(names) - 0.3)
    ax.legend(frameon=False, loc="lower right", fontsize=9)
    _style(ax, "Response time by endpoint — 50 concurrent users", "milliseconds (log scale)")
    ax.grid(axis="x", lw=0.8, alpha=0.9)
    fig.tight_layout()
    fig.savefig(RESULTS / "response_times.png")


def chart_queue(rows):
    """Queue depth against settlements completed, both counts, one axis."""
    t = [float(r["t"]) for r in rows]
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
    _style(ax, "One worker cannot keep up with the web tier", "seconds into the run")
    ax.set_ylabel("messages", color=MUTED)
    fig.tight_layout()
    fig.savefig(RESULTS / "queue_depth.png")


def chart_throughput(one_worker, four_workers, xrpl):
    """Settlements per minute: measured (simulated ledger) and the real-ledger rate."""
    real_payment = xrpl["payment"]["mean_s"]
    labels = ["1 worker\n(simulated)", "4 workers\n(simulated)", "1 worker\n(real ledger)", "4 workers\n(real ledger)"]
    values = [one_worker, four_workers, 60 / real_payment, 60 / real_payment * 4]
    colors = [BLUE, BLUE, ORANGE, ORANGE]

    fig, ax = plt.subplots(figsize=(7, 4))
    bars = ax.bar(labels, values, color=colors, width=0.56)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.02,
                f"{value:.0f}", ha="center", fontsize=9, color=MUTED)
    ax.set_ylim(0, max(values) * 1.15)
    ax.set_ylabel("settlements per minute", color=MUTED)
    _style(ax, "Settlement throughput scales with workers")
    fig.text(0.01, 0.01, f"Real-ledger bars assume the measured {real_payment:.0f}s payment and "
                          "ignore any Testnet rate limit.", fontsize=8, color=MUTED)
    fig.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(RESULTS / "throughput.png")


def rate_per_minute(rows):
    span = float(rows[-1]["t"]) - float(rows[0]["t"])
    done = int(rows[-1]["completed"]) - int(rows[0]["completed"])
    return done / span * 60 if span else 0.0


def markdown_tables(stats, xrpl):
    print("### API response times (50 concurrent users)\n")
    print("| Endpoint | Requests | Median (ms) | p95 (ms) | Max (ms) | Failures |")
    print("| --- | ---: | ---: | ---: | ---: | ---: |")
    for row in sorted(stats, key=lambda r: -int(r["Request Count"])):
        if int(row["Request Count"]) < 20 and row["Name"] != "Aggregated":
            continue
        name = row["Name"] if row["Name"] != "Aggregated" else "**All requests**"
        print(f"| {name} | {int(row['Request Count']):,} | {float(row['50%']):,.0f} | "
              f"{float(row['95%']):,.0f} | {float(row['Max Response Time']):,.0f} | {row['Failure Count']} |")

    print("\n### Real XRPL Testnet timings\n")
    print("| Step | Samples | Min (s) | Mean (s) | Max (s) |")
    print("| --- | ---: | ---: | ---: | ---: |")
    for step in ("faucet_account", "trust_line", "payment"):
        data = xrpl[step]
        label = {"faucet_account": "Create recipient account (faucet)",
                 "trust_line": "TrustSet to issuer",
                 "payment": "Treasury → recipient payment"}[step]
        print(f"| {label} | {data['samples']} | {data['min_s']} | {data['mean_s']} | {data['max_s']} |")
    print(f"| **First transfer to a new recipient** | 1 | — | {xrpl['first_transfer_total_s']} | — |")


def main() -> None:
    stats = _read("run_stats.csv")
    queue_1 = _read("queue.csv")
    queue_4 = _read("queue_4workers.csv")
    xrpl = json.loads((RESULTS / "xrpl_timings.json").read_text())

    chart_response_times(stats)
    chart_queue(queue_1)
    chart_throughput(rate_per_minute(queue_1), rate_per_minute(queue_4), xrpl)
    markdown_tables(stats, xrpl)

    print(f"\n1 worker: {rate_per_minute(queue_1):.1f} settlements/min "
          f"| 4 workers: {rate_per_minute(queue_4):.1f}/min (simulated ledger)")
    print("Charts written to perf/results/*.png")


if __name__ == "__main__":
    main()
