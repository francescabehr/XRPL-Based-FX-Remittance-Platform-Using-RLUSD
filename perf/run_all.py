"""
Phase 9: every performance figure from one command (`make perf-all`).

One session, fixed order, fresh state before every sub-run, so a rerun repeats
the method and its numbers are comparable:

  1. preflight   the venv interpreter, its locust/faker/matplotlib, a free port
  2. clean       delete perf/results/* (xrpl_timings.json kept unless re-measured)
  3. database    drop + recreate a DEDICATED perf database, migrate, seed the admin
  4. load runs   reference (50 users, 3 min) then tiers 10/25/50/100 (2 min each).
                 Before each: stop everything, flush the perf Redis DB (the queue),
                 purge + re-seed the synthetic users (so no run inherits another's
                 spent daily limits), start uvicorn (no --reload), one simulated
                 worker and the queue sampler. After each: snapshot DB outcomes.
  5. scaling     the same pre-filled backlog drained by 1 worker, then by 4
  6. real XRPL   perf/measure_xrpl.py, ONLY with PERF_REAL_XRPL=1 (spends Testnet UCTUSD)
  7. analyze     perf/analyze.py -> charts + perf/results/summary.md
  8. metadata    perf/results/run_meta.json, with a manifest of every file written

Isolation. The run never touches the app's own database or queue. It uses its
own Postgres database (PERF_DB_NAME, default remittance_perf, recreated each run
and kept afterwards for inspection) and its own Redis DB (PERF_REDIS_URL, default
redis://localhost:6379/14). Both are refused if they match the app's configured
DATABASE_URL / REDIS_URL or the test database. A separate Redis DB also means a
real `make worker` listening on the app's queue can never pick up a load-test job.

The workers are perf/perf_worker.py (simulated ledger). Nothing is submitted to
the XRPL unless PERF_REAL_XRPL=1. GET /wallet still makes its usual read-only
balance lookup against Testnet.

Parameters (environment, defaults in brackets):
  PERF_DB_NAME [remittance_perf]   PERF_REDIS_URL [redis://localhost:6379/14]
  PERF_PORT [8765]                 PERF_PAIRS [200]      PERF_SPAWN_RATE [5]
  PERF_REF_USERS [50]              PERF_REF_TIME [3m]
  PERF_TIERS [10,25,50,100]        PERF_TIER_TIME [2m]
  PERF_BACKLOG [30]                PERF_SCALE_WORKERS [1,4]
  PERF_DRAIN_TIMEOUT [900] s       PERF_XRPL_LATENCY [4.0] s
  PERF_REAL_XRPL [0]               PERF_XRPL_PAYMENTS [3]
  PERF_RESULTS_DIR [perf/results]  (for smoke-testing this script only)

Usage: make perf-all          (or .venv/bin/python perf/run_all.py)
"""

import asyncio
import importlib.util
import json
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
BIN = VENV / "bin"
PY = BIN / "python"
# Overridable only so the runner itself can be smoke-tested without touching perf/results.
RESULTS = Path(os.environ.get("PERF_RESULTS_DIR", ROOT / "perf" / "results")).resolve()
LOGS = RESULTS / "logs"
META = RESULTS / "run_meta.json"
XRPL_TIMINGS = RESULTS / "xrpl_timings.json"
SEED_PATTERN = "perf.%@loadtest.local"  # matches perf/seed_data.py PATTERN


def die(message: str) -> None:
    print(f"\nperf-all: {message}", file=sys.stderr)
    sys.exit(1)


# --- 1. preflight -------------------------------------------------------------

def preflight() -> None:
    install = f"Install with:  {PY} -m pip install -r requirements.txt"
    if not PY.exists():
        die(f"no virtualenv at {VENV}. Create it with `python3 -m venv .venv`, then\n  {install}")
    # Refuses to run under any other interpreter (e.g. a shell alias to Homebrew Python).
    if Path(sys.prefix).resolve() != VENV.resolve():
        die(f"run this with {PY}, not {sys.executable}. `make perf-all` does that for you.")
    missing = [m for m in ("locust", "faker", "matplotlib", "uvicorn", "rq", "alembic")
               if importlib.util.find_spec(m) is None]
    if missing:
        die(f".venv is missing: {', '.join(missing)}.\n  {install}")
    for tool in ("locust", "uvicorn", "alembic"):
        if not (BIN / tool).exists():
            die(f"{BIN / tool} not found.\n  {install}")


def env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def env_list(name: str, default: str) -> list[int]:
    return [int(v) for v in os.environ.get(name, default).split(",") if v.strip()]


def seconds(duration: str) -> int:
    """Locust-style duration (90, 90s, 3m, 1h30m) in seconds."""
    parts = re.findall(r"(\d+)([hms]?)", duration)
    if not parts or "".join(n + u for n, u in parts) != duration:
        die(f"bad duration {duration!r}; use e.g. 10s, 2m, 3m")
    return sum(int(n) * {"h": 3600, "m": 60, "s": 1, "": 1}[u] for n, u in parts)


preflight()
sys.path.insert(0, str(ROOT))
# Read before the environment is overridden for the children.
from sqlalchemy.engine import make_url  # noqa: E402

from app.config import settings  # noqa: E402

PARAMS = {
    "db_name": os.environ.get("PERF_DB_NAME", "remittance_perf"),
    "redis_url": os.environ.get("PERF_REDIS_URL", "redis://localhost:6379/14"),
    "port": env_int("PERF_PORT", 8765),
    "pairs": env_int("PERF_PAIRS", 200),
    "spawn_rate": env_int("PERF_SPAWN_RATE", 5),
    "ref_users": env_int("PERF_REF_USERS", 50),
    "ref_time": os.environ.get("PERF_REF_TIME", "3m"),
    "tiers": env_list("PERF_TIERS", "10,25,50,100"),
    "tier_time": os.environ.get("PERF_TIER_TIME", "2m"),
    "backlog": env_int("PERF_BACKLOG", 30),
    "scale_workers": env_list("PERF_SCALE_WORKERS", "1,4"),
    "drain_timeout_s": env_int("PERF_DRAIN_TIMEOUT", 900),
    "xrpl_latency_s": float(os.environ.get("PERF_XRPL_LATENCY", "4.0")),
    "real_xrpl": os.environ.get("PERF_REAL_XRPL", "0") == "1",
    "xrpl_payments": env_int("PERF_XRPL_PAYMENTS", 3),
    "server": "uvicorn app.main:app, 1 process, no --reload, --no-access-log",
    "worker": "perf/perf_worker.py (SimpleWorker, simulated ledger)",
}
for key in ("ref_time", "tier_time"):
    seconds(PARAMS[key])

APP_DB = make_url(settings.database_url)
PERF_DB = APP_DB.set(database=PARAMS["db_name"])
HOST = f"http://127.0.0.1:{PARAMS['port']}"


def guard_targets() -> None:
    """Refuse any target that is the app's real database or queue."""
    protected = {APP_DB.database, make_url(settings.test_database_url).database, "postgres"}
    if PARAMS["db_name"] in protected:
        die(f"PERF_DB_NAME={PARAMS['db_name']!r} is a protected database; use a dedicated one.")
    perf_redis, app_redis = PARAMS["redis_url"].rstrip("/"), settings.redis_url.rstrip("/")
    redis_db = perf_redis.rsplit("/", 1)[-1]
    if perf_redis == app_redis or not redis_db.isdigit() or redis_db == "0":
        die(f"PERF_REDIS_URL={perf_redis!r} must name its own Redis DB (not 0, not the app's {app_redis}).")
    if PARAMS["real_xrpl"] and RESULTS != (ROOT / "perf" / "results").resolve():
        die("PERF_REAL_XRPL=1 writes perf/results/xrpl_timings.json; don't combine it with PERF_RESULTS_DIR.")
    with socket.socket() as sock:
        if sock.connect_ex(("127.0.0.1", PARAMS["port"])) == 0:
            die(f"port {PARAMS['port']} is in use; stop that server or set PERF_PORT.")


ENV = {
    **os.environ,
    "DATABASE_URL": PERF_DB.render_as_string(hide_password=False),
    "REDIS_URL": PARAMS["redis_url"],
    "PERF_XRPL_LATENCY": str(PARAMS["xrpl_latency_s"]),
    "PERF_PAIRS": str(PARAMS["pairs"]),
    "ADMIN_EMAIL": settings.admin_email,
    "ADMIN_PASSWORD": settings.admin_password,
    "PERF_RESULTS_DIR": str(RESULTS),
    "PYTHONUNBUFFERED": "1",
}

META_DATA: dict = {}
CHILDREN: list[subprocess.Popen] = []


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def save_meta() -> None:
    META.write_text(json.dumps(META_DATA, indent=2, default=str))


def log(message: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {message}", flush=True)


# --- processes ----------------------------------------------------------------

def run(cmd: list, label: str, timeout: int = 600) -> int:
    """Run to completion, output to logs/<label>.log. Returns the exit code."""
    with (LOGS / f"{label}.log").open("a") as out:
        return subprocess.run([str(c) for c in cmd], cwd=ROOT, env=ENV, stdout=out,
                              stderr=subprocess.STDOUT, timeout=timeout).returncode


def run_checked(cmd: list, label: str) -> None:
    code = run(cmd, label)
    if code:
        raise RuntimeError(f"{label} exited {code}; see {LOGS}/{label}.log")


def start(cmd: list, label: str) -> subprocess.Popen:
    out = (LOGS / f"{label}.log").open("a")
    proc = subprocess.Popen([str(c) for c in cmd], cwd=ROOT, env=ENV, stdout=out,
                            stderr=subprocess.STDOUT, start_new_session=True)
    proc.label = label  # type: ignore[attr-defined]
    CHILDREN.append(proc)
    return proc


def stop(proc: subprocess.Popen, sig=signal.SIGINT, grace: int = 30) -> bool:
    """Stop a child; returns True if it was still alive (i.e. had not crashed)."""
    alive = proc.poll() is None
    if alive:
        proc.send_signal(sig)
        try:
            proc.wait(grace)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    if proc in CHILDREN:
        CHILDREN.remove(proc)
    return alive


def stop_all() -> None:
    for proc in list(CHILDREN):
        stop(proc, signal.SIGTERM, grace=10)


def on_signal(signum, _frame) -> None:
    stop_all()
    META_DATA["status"] = f"aborted (signal {signum})"
    META_DATA["finished_at"] = now_iso()
    save_meta()
    die("interrupted; child processes stopped.")


# --- state --------------------------------------------------------------------

def redis_client():
    from redis import Redis
    return Redis.from_url(PARAMS["redis_url"])


async def _sql(url, statements: list[str], autocommit: bool = False) -> list:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url, isolation_level="AUTOCOMMIT" if autocommit else None)
    try:
        results = []
        async with engine.connect() as conn:
            for statement in statements:
                result = await conn.execute(text(statement))
                results.append(result.all() if result.returns_rows else None)
        return results
    finally:
        await engine.dispose()


def sql(statements: list[str], url=None, autocommit: bool = False) -> list:
    return asyncio.run(_sql(url or PERF_DB, statements, autocommit))


def recreate_database() -> None:
    name = PARAMS["db_name"]
    sql([f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', f'CREATE DATABASE "{name}"'],
        url=APP_DB.set(database="postgres"), autocommit=True)
    run_checked([BIN / "alembic", "upgrade", "head"], "setup")
    run_checked([PY, "-m", "app.scripts.seed_admin"], "setup")


def reset_state(label: str) -> None:
    """Empty queue, fresh synthetic users: every sub-run starts from the same place."""
    redis_client().flushdb()  # the perf-only Redis DB (guarded above)
    run_checked([PY, "perf/seed_data.py", "--purge"], f"{label}_seed")
    run_checked([PY, "perf/seed_data.py", str(PARAMS["pairs"])], f"{label}_seed")


def wait_for_server(proc: subprocess.Popen) -> None:
    deadline = time.time() + 60
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"uvicorn exited {proc.returncode}; see {LOGS}/{proc.label}.log")
        try:
            with urllib.request.urlopen(f"{HOST}/login", timeout=2) as response:
                if response.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError("uvicorn did not answer /login within 60 s")


def start_workers(count: int, label: str) -> list[subprocess.Popen]:
    from rq import Worker

    workers = [start([PY, "perf/perf_worker.py"], f"{label}_worker{i + 1}") for i in range(count)]
    deadline = time.time() + 30
    while Worker.count(connection=redis_client()) < count:
        if time.time() > deadline or any(w.poll() is not None for w in workers):
            raise RuntimeError(f"{count} worker(s) did not register; see {LOGS}/{label}_worker*.log")
        time.sleep(0.2)
    return workers


def snapshot_outcomes(label: str) -> dict:
    """Cash-in / settlement / cash-out counts for the synthetic users, at the end of a sub-run."""
    perf_sender = f"sender_id IN (SELECT id FROM users WHERE email LIKE '{SEED_PATTERN}')"
    perf_recipient = f"recipient_user_id IN (SELECT id FROM users WHERE email LIKE '{SEED_PATTERN}')"
    pairs, attempts, cashouts = sql([
        f"SELECT cashin_status::text, settlement_status::text, count(*) FROM transactions "
        f"WHERE {perf_sender} GROUP BY 1, 2 ORDER BY 1, 2",
        f"SELECT count(*) FILTER (WHERE settlement_status = 'completed' AND xrpl_tx_hash IS NULL), "
        f"coalesce(max(settlement_attempts), 0), coalesce(avg(settlement_attempts), 0) "
        f"FROM transactions WHERE {perf_sender} AND cashin_status = 'received'",
        f"SELECT status::text, count(*) FROM cashout_requests WHERE {perf_recipient} GROUP BY 1 ORDER BY 1",
    ])
    outcome = {
        "run_id": META_DATA["run_id"], "sub_run": label, "taken_at": now_iso(),
        "cashin_by_settlement": [{"cashin": c, "settlement": s, "count": n} for c, s, n in pairs],
        "completed_without_hash": attempts[0][0],
        "settlement_attempts_max": attempts[0][1],
        "settlement_attempts_mean": round(float(attempts[0][2]), 3),
        "cashout_by_status": {s: n for s, n in cashouts},
    }
    (RESULTS / f"{label}_outcomes.json").write_text(json.dumps(outcome, indent=2))
    return outcome


def log_errors(label_prefix: str) -> dict:
    """Tracebacks in this sub-run's logs, by log file."""
    found = {}
    for path in LOGS.glob(f"{label_prefix}_*.log"):
        count = path.read_text(errors="replace").count("Traceback (most recent call last)")
        if count:
            found[path.name] = count
    return found


# --- 4. load runs ---------------------------------------------------------------

def load_run(label: str, users: int, duration: str) -> None:
    log(f"{label}: {users} users for {duration}")
    record = META_DATA["sub_runs"][label] = {
        "kind": "load", "users": users, "duration": duration, "spawn_rate": PARAMS["spawn_rate"],
        "workers": 1, "started_at": now_iso(),
    }
    save_meta()
    reset_state(label)
    server = start([BIN / "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", PARAMS["port"],
                    "--no-access-log"], f"{label}_server")
    wait_for_server(server)
    workers = start_workers(1, label)
    sampler = start([PY, "perf/sampler.py", RESULTS / f"queue_{label}.csv"], f"{label}_sampler")
    time.sleep(1)

    record["locust_started_at"] = now_iso()
    code = run([BIN / "locust", "-f", "perf/locustfile.py", "--headless", "-u", users,
                "-r", PARAMS["spawn_rate"], "-t", duration, "-H", HOST, "--csv", RESULTS / label],
               f"{label}_locust", timeout=seconds(duration) + 300)
    record["locust_finished_at"] = now_iso()
    # Locust exits 1 when any request was marked failed (e.g. a spent daily limit);
    # that is data, not a crash. Anything else is an error.
    record["locust_exit_code"] = code

    record["sampler_alive_at_end"] = stop(sampler)
    record["workers_alive_at_end"] = all([stop(w, signal.SIGTERM) for w in workers])
    record["server_alive_at_end"] = stop(server)
    record["outcomes"] = snapshot_outcomes(label)
    record["tracebacks"] = log_errors(label)
    record["finished_at"] = now_iso()
    check_sub_run(label, record, expect=[f"{label}_stats.csv", f"{label}_failures.csv",
                                         f"{label}_exceptions.csv", f"queue_{label}.csv"])
    save_meta()


# --- 5. worker scaling ----------------------------------------------------------

def drain_run(count: int) -> None:
    from rq import Queue

    label = f"workers{count}"
    backlog = PARAMS["backlog"]
    log(f"{label}: draining a {backlog}-settlement backlog with {count} worker(s)")
    record = META_DATA["sub_runs"][label] = {
        "kind": "drain", "workers": count, "backlog": backlog, "started_at": now_iso(),
    }
    save_meta()
    reset_state(label)
    run_checked([PY, "perf/backlog.py", backlog], f"{label}_backlog")
    record["queued_before_start"] = len(Queue("settlement", connection=redis_client()))

    sampler = start([PY, "perf/sampler.py", RESULTS / f"queue_{label}.csv"], f"{label}_sampler")
    time.sleep(1.5)
    workers = start_workers(count, label)
    record["workers_ready_epoch"] = round(time.time(), 2)

    done_sql = ("SELECT count(*) FROM transactions WHERE settlement_status IN ('completed', 'failed') "
                f"AND sender_id IN (SELECT id FROM users WHERE email LIKE '{SEED_PATTERN}')")
    deadline = time.time() + PARAMS["drain_timeout_s"]
    while (done := sql([done_sql])[0][0][0]) < backlog:
        if time.time() > deadline:
            record["timed_out"] = True
            break
        time.sleep(0.5)
    record["drained_epoch"] = round(time.time(), 2)
    record["settled_or_failed"] = done
    time.sleep(1.5)  # let the sampler record the final state

    record["sampler_alive_at_end"] = stop(sampler)
    record["workers_alive_at_end"] = all([stop(w, signal.SIGTERM) for w in workers])
    record["outcomes"] = snapshot_outcomes(label)
    record["tracebacks"] = log_errors(label)
    record["finished_at"] = now_iso()
    check_sub_run(label, record, expect=[f"queue_{label}.csv"])
    save_meta()


def check_sub_run(label: str, record: dict, expect: list[str]) -> None:
    problems = [f"missing {name}" for name in expect if not (RESULTS / name).exists()]
    if record.get("locust_exit_code") not in (None, 0, 1):
        problems.append(f"locust exit code {record['locust_exit_code']}")
    for key in ("server_alive_at_end", "workers_alive_at_end"):
        if record.get(key) is False:
            problems.append(f"{key.split('_')[0]} died during the run")
    if record.get("timed_out"):
        problems.append("backlog did not drain before PERF_DRAIN_TIMEOUT")
    exceptions = RESULTS / f"{label}_exceptions.csv"
    if exceptions.exists() and len(exceptions.read_text().strip().splitlines()) > 1:
        problems.append(f"python exceptions in {exceptions.name}")
    problems += [f"{n} traceback(s) in logs/{name}" for name, n in record["tracebacks"].items()]
    record["problems"] = problems
    if problems:
        META_DATA["errors"].extend(f"{label}: {p}" for p in problems)
        log(f"{label}: PROBLEMS: {'; '.join(problems)}")


# --- metadata -------------------------------------------------------------------

def output(cmd: list) -> str:
    try:
        return subprocess.run([str(c) for c in cmd], cwd=ROOT, capture_output=True, text=True,
                              timeout=30).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable"


def environment() -> dict:
    from importlib.metadata import version

    mem = output(["sysctl", "-n", "hw.memsize"])
    return {
        "git_commit": output(["git", "rev-parse", "HEAD"]),
        "git_branch": output(["git", "rev-parse", "--abbrev-ref", "HEAD"]),
        "git_dirty_files": output(["git", "status", "--porcelain"]).splitlines(),
        "python": sys.version.split()[0],
        "python_executable": sys.executable,
        "postgres": sql(["SELECT version()"])[0][0][0],
        "redis": redis_client().info("server")["redis_version"],
        "packages": {p: version(p) for p in ("locust", "uvicorn", "fastapi", "sqlalchemy", "asyncpg",
                                              "rq", "redis", "xrpl-py", "faker", "matplotlib")},
        "machine": {
            "platform": platform.platform(),
            "arch": platform.machine(),
            "cpu": output(["sysctl", "-n", "machdep.cpu.brand_string"]),
            "cpu_count": os.cpu_count(),
            "memory_gb": round(int(mem) / 2**30, 1) if mem.isdigit() else "unavailable",
        },
        "same_machine": "app, Postgres, Redis, workers and Locust all on this host",
    }


def manifest() -> list[dict]:
    files = []
    for path in sorted(RESULTS.rglob("*")):
        if path.is_file() and path != META:
            rel = path.relative_to(RESULTS).as_posix()
            files.append({"file": rel, "bytes": path.stat().st_size,
                          "modified": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                          .isoformat(timespec="seconds")})
    return files


# --- main -----------------------------------------------------------------------

def clean_results() -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    for path in RESULTS.iterdir():
        if path == XRPL_TIMINGS and not PARAMS["real_xrpl"]:
            continue
        shutil.rmtree(path) if path.is_dir() else path.unlink()
    LOGS.mkdir()


def main() -> None:
    guard_targets()
    clean_results()
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    started = datetime.now(timezone.utc)
    META_DATA.update({
        "run_id": started.strftime("%Y%m%dT%H%M%SZ"), "started_at": started.isoformat(timespec="seconds"),
        "status": "running", "parameters": PARAMS, "errors": [], "sub_runs": {},
    })
    save_meta()
    log(f"perf-all {META_DATA['run_id']}: database {PARAMS['db_name']}, Redis {PARAMS['redis_url']}, {HOST}")

    try:
        recreate_database()
        META_DATA["environment"] = environment()
        save_meta()

        load_run("run", PARAMS["ref_users"], PARAMS["ref_time"])
        for users in PARAMS["tiers"]:
            load_run(f"c{users}", users, PARAMS["tier_time"])
        for count in PARAMS["scale_workers"]:
            drain_run(count)

        if PARAMS["real_xrpl"]:
            log(f"real XRPL: 1 account + {PARAMS['xrpl_payments']} treasury payments on Testnet")
            code = run([PY, "perf/measure_xrpl.py", PARAMS["xrpl_payments"]], "xrpl", timeout=900)
            META_DATA["xrpl_timings"] = {"source": "measured in this run", "exit_code": code}
            if code:
                META_DATA["errors"].append(f"measure_xrpl.py exited {code}")
        else:
            modified = (datetime.fromtimestamp(XRPL_TIMINGS.stat().st_mtime, timezone.utc)
                        .isoformat(timespec="seconds") if XRPL_TIMINGS.exists() else None)
            META_DATA["xrpl_timings"] = {
                "source": "carried over from an earlier run (PERF_REAL_XRPL not set)" if modified
                else "none: xrpl_timings.json absent and PERF_REAL_XRPL not set",
                "file_modified": modified,
                "file_last_commit": (output(["git", "log", "-1", "--format=%h %ad", "--date=short", "--",
                                             "perf/results/xrpl_timings.json"]) or None) if modified else None,
            }
        save_meta()

        # Final status before analyze, so summary.md carries it; analyze failing
        # afterwards still downgrades it below.
        META_DATA["measurements_finished_at"] = now_iso()
        META_DATA["status"] = "complete_with_errors" if META_DATA["errors"] else "complete"
        save_meta()
        log("analyze: charts + summary.md")
        if run([PY, "perf/analyze.py"], "analyze"):
            META_DATA["errors"].append("analyze.py failed; see logs/analyze.log")
            META_DATA["status"] = "complete_with_errors"
    except Exception as exc:
        META_DATA["status"] = "failed"
        META_DATA["errors"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        stop_all()
        try:
            redis_client().flushdb()  # leave no load-test jobs behind in the perf Redis DB
        except Exception:  # noqa: BLE001
            pass
        META_DATA["finished_at"] = now_iso()
        META_DATA["files"] = manifest()
        save_meta()
        log(f"perf-all {META_DATA['status']}. Metadata: {META}")
        for error in META_DATA["errors"]:
            log(f"  ! {error}")


if __name__ == "__main__":
    main()
