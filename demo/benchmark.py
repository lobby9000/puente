"""Chaos benchmark: same data, same failure rates, a naive script against Puente.

    python -m demo.benchmark          # full run: 850 reps x 12 months, about 3 minutes
    python -m demo.benchmark --quick  # small run for a smoke test

It starts the mock SaaS in a subprocess, turns on failure injection, runs each
contender against an empty destination, then audits the destination with
failure injection off and compares every business key with the ground truth.
Results go to reports/chaos_report.md and reports/chaos_report.json.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from collections import defaultdict
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import httpx

from demo.generate import SOURCE_FILE, generate
from demo.naive import run_naive
from puente.breaker import CircuitBreaker
from puente.client import SaaSClient
from puente.log import setup_logging
from puente.reconcile import reconcile
from puente.retry import RetryPolicy
from puente.sources import CsvSource, SourceRow
from puente.state import StateStore
from puente.sync import run_sync

ROOT = Path(__file__).resolve().parent.parent
CLIENT_ID, CLIENT_SECRET = "puente-dev", "dev-secret"
INJECTED = ("injected_errors", "injected_ghost_writes", "injected_slow", "injected_auth_errors")


def scenario(quick: bool) -> dict:
    return {
        "error_rate": 0.09,  # 500, 502 or 503 on any call
        "ghost_write_rate": 0.05,  # batch written, answer lost (504)
        "slow_rate": 0.05,  # answer held 3 s, past the 2 s client timeout
        "slow_seconds": 3.0,
        "auth_error_rate": 0.02,  # token revoked mid-run
        "outage_at_request": 4 if quick else 30,  # this batch POST takes the service down...
        "outage_seconds": 10 if quick else 30,  # ...for this long
        "seed": 2026,
    }


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def mock_server(payee_count: int):
    port = free_port()
    env = {
        **os.environ,
        "MOCK_PAYEE_COUNT": str(payee_count),
        "MOCK_TOKEN_TTL_SECONDS": "20",
        "MOCK_RATE_LIMIT_CAPACITY": "20",
        "MOCK_RATE_LIMIT_REFILL_PER_S": "10",
    }
    command = [sys.executable, "-m", "uvicorn", "mock_saas.app:app", "--app-dir", str(ROOT), "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    process = subprocess.Popen(command, env=env, cwd=ROOT)
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if httpx.get(f"{url}/health", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError("the mock SaaS did not start")
            time.sleep(0.2)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()


def admin(url: str, method: str, path: str, payload: dict | None = None) -> dict:
    response = httpx.request(method, f"{url}{path}", json=payload, timeout=10)
    response.raise_for_status()
    return response.json()


def run_contender(name: str, url: str, rows: list[SourceRow], chaos: dict, state_dir: Path) -> dict:
    admin(url, "POST", "/_admin/reset")
    admin(url, "PUT", "/_admin/chaos", chaos)
    started = time.monotonic()
    review: list[dict] = []
    if name == "naive":
        details = run_naive(url, CLIENT_ID, CLIENT_SECRET, rows)
    else:
        db = state_dir / "puente.db"
        db.unlink(missing_ok=True)
        store = StateStore(db)
        policy = RetryPolicy(max_attempts=5, max_elapsed_s=15, seed=1)
        breaker = CircuitBreaker(failure_threshold=3, cool_down_s=5, max_open_s=90)
        with SaaSClient(url, CLIENT_ID, CLIENT_SECRET, timeout_s=2.0, retry=policy) as client:
            summary = run_sync(client, store, rows, source="benchmark", batch_size=100, breaker=breaker)
        details = {"sync": summary.as_dict(), "replay": summary.replay.as_dict() if summary.replay else None}
        review = store.open_items()
        store.close()
    elapsed = round(time.monotonic() - started, 1)
    stats = admin(url, "GET", "/_admin/stats")["stats"]
    admin(url, "PUT", "/_admin/chaos", {})  # the auditor reads with failure injection off
    with SaaSClient(url, CLIENT_ID, CLIENT_SECRET) as auditor:
        payouts = list(auditor.iter_payouts())
    return {"name": name, "elapsed_s": elapsed, "details": details, "stats": stats, "payouts": payouts, "review": review}


def score(result: dict, rows: list[SourceRow], truth: dict) -> dict:
    audit = reconcile(rows, result["payouts"], result["review"])
    paid: dict[str, list[dict]] = defaultdict(list)
    for payout in result["payouts"]:
        paid[payout["external_id"]].append(payout)
    parked = {item["external_id"]: item["reason"] for item in result["review"]}
    payable = [key for key, outcome in truth["keys"].items() if outcome == "paid"]
    problems = {key: outcome.split(":", 1)[1] for key, outcome in truth["keys"].items() if outcome != "paid"}
    wrong = set(audit.wrong_amount)
    stats = result["stats"]
    return {
        "payable": len(payable),
        "delivered_once": sum(1 for key in payable if len(paid.get(key, ())) == 1 and key not in wrong),
        "payable_lost": sum(1 for key in payable if not paid.get(key) and key not in parked),
        "payable_waiting": sum(1 for key in payable if not paid.get(key) and key in parked),
        "keys_paid_twice": sum(1 for found in paid.values() if len(found) > 1),
        "extra_payouts": sum(len(found) - 1 for found in paid.values() if len(found) > 1),
        "problems": len(problems),
        "problems_sent": sum(1 for key in problems if paid.get(key)),
        "problems_right_reason": sum(1 for key, reason in problems.items() if not paid.get(key) and parked.get(key) == reason),
        "problems_vanished": sum(1 for key in problems if not paid.get(key) and key not in parked),
        "audit_clean": audit.clean and not audit.not_in_source,
        "requests": stats.get("requests", 0),
        "batch_requests": stats.get("batch_requests", 0),
        "injected": sum(stats.get(name, 0) for name in INJECTED),
        "injected_by_kind": {name: stats.get(name, 0) for name in INJECTED},
        "outage_rejections": stats.get("outage_rejections", 0),
        "rate_limited": stats.get("rate_limited", 0),
        "elapsed_s": result["elapsed_s"],
    }


def share(part: int, whole: int) -> str:
    return f"{part:,} of {whole:,} ({part / whole:.1%})" if whole else "0"


def render(meta: dict, chaos: dict, naive: dict, puente: dict, extra: dict) -> str:
    rows = [
        ("Payable rows delivered exactly once", share(naive["delivered_once"], naive["payable"]), share(puente["delivered_once"], puente["payable"])),
        ("Payable rows lost without a record", f"{naive['payable_lost']:,}", f"{puente['payable_lost']:,}"),
        ("Payable rows still waiting in review", f"{naive['payable_waiting']:,}", f"{puente['payable_waiting']:,}"),
        ("Rows paid twice (extra payouts)", f"{naive['keys_paid_twice']:,} ({naive['extra_payouts']:,})", f"{puente['keys_paid_twice']:,} ({puente['extra_payouts']:,})"),
        ("Defective rows sent downstream", f"{naive['problems_sent']:,}", f"{puente['problems_sent']:,}"),
        ("Defective rows parked with the right reason", share(naive["problems_right_reason"], naive["problems"]), share(puente["problems_right_reason"], puente["problems"])),
        ("Defective rows that vanished without a record", f"{naive['problems_vanished']:,}", f"{puente['problems_vanished']:,}"),
        ("Wall time", f"{naive['elapsed_s']:.0f} s", f"{puente['elapsed_s']:.0f} s"),
    ]
    table = "\n".join(f"| {label} | {left} | {right} |" for label, left, right in rows)
    kinds = puente["injected_by_kind"]
    return f"""# Chaos benchmark

Run on {meta['date']} with data seed {meta['seed']}: {meta['rows']:,} source rows ({meta['vendors']} sales reps x {meta['months']} months, plus injected defects), {meta['payable']:,} payable business keys and {meta['problems']:,} defective ones.

Failure injection, the same for both contenders: {chaos['error_rate']:.0%} of calls get a transient 5xx and {chaos['auth_error_rate']:.0%} hit a revoked token. {chaos['ghost_write_rate']:.0%} of batch writes are answered with a 504 after the write landed, and {chaos['slow_rate']:.0%} are answered {chaos['slow_seconds']:g} s late, past the 2 s client timeout. The rate limit is 10 calls per second, and batch request number {chaos['outage_at_request']} starts a {chaos['outage_seconds']:g} s outage.

| | Naive script | Puente |
|---|---:|---:|
{table}

During Puente's run the mock SaaS refused {puente['injected']:,} of {puente['requests']:,} calls by design ({kinds['injected_errors']:,} transient errors, {kinds['injected_ghost_writes']:,} lost answers, {kinds['injected_slow']:,} slow answers, {kinds['injected_auth_errors']:,} revoked tokens), {puente['outage_rejections']:,} more during the outage and {puente['rate_limited']:,} for the rate limit. Puente retried {extra['retries']:,} times, opened the circuit breaker {extra['breaker_opened']} time(s), and its automatic replay found {extra['already_in_destination']:,} parked lines already in the destination (written before their answer was lost) and delivered {extra['replay_delivered']:,} more.

Audit of the destination after Puente's run, with failure injection off: {"clean, every key delivered once or parked with a reason" if puente["audit_clean"] else "NOT clean"}.

The naive script sends batches of 100, tries each request 3 times with a fixed 0.5 s pause, uses a new Idempotency-Key per attempt and validates nothing first. See demo/naive.py.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the chaos benchmark: a naive script against Puente.")
    parser.add_argument("--vendors", type=int, default=850)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--quick", action="store_true", help="150 reps x 4 months and a short outage")
    parser.add_argument("--out", default="reports")
    args = parser.parse_args(argv)
    if args.quick:
        args.vendors, args.months = 150, 4

    setup_logging("logs/benchmark.jsonl", console_level=logging.ERROR)
    chaos = scenario(args.quick)
    data_dir, state_dir, out_dir = Path("data/benchmark"), Path("state/benchmark"), Path(args.out)
    state_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    truth = generate(args.vendors, args.months, args.seed, data_dir)
    rows = list(CsvSource(data_dir / SOURCE_FILE).rows())
    print(f"data: {len(rows):,} rows, {truth['key_count']:,} business keys")

    results = {}
    with mock_server(args.vendors) as url:
        for name in ("naive", "puente"):
            print(f"running {name}...", flush=True)
            results[name] = run_contender(name, url, rows, chaos, state_dir)
            print(f"  done in {results[name]['elapsed_s']:.0f} s", flush=True)

    scores = {name: score(result, rows, truth) for name, result in results.items()}
    sync = results["puente"]["details"]["sync"]
    replay = results["puente"]["details"]["replay"] or {}
    extra = {
        "retries": sync["retries"] + replay.get("retries", 0),
        "breaker_opened": sync["breaker_opened"],
        "already_in_destination": replay.get("already_in_destination", 0),
        "replay_delivered": replay.get("lines_delivered", 0),
    }
    meta = {
        "date": date.today().isoformat(),
        "seed": args.seed,
        "rows": len(rows),
        "vendors": args.vendors,
        "months": args.months,
        "payable": scores["puente"]["payable"],
        "problems": scores["puente"]["problems"],
    }
    report = render(meta, chaos, scores["naive"], scores["puente"], extra)
    (out_dir / "chaos_report.md").write_text(report, encoding="utf-8")
    dump = {
        "meta": meta,
        "chaos": chaos,
        "scores": scores,
        "puente": results["puente"]["details"],
        "naive": results["naive"]["details"],
        "expected": truth["expected"],
    }
    (out_dir / "chaos_report.json").write_text(json.dumps(dump, indent=2, default=str), encoding="utf-8")
    print()
    print(report)
    return 0 if scores["puente"]["audit_clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
