"""Command line.

    python -m puente sync --source SOURCE       deliver rows, then replay transient failures
    python -m puente replay                     retry review items that failed for transient reasons
    python -m puente review                     show the open review queue
    python -m puente runs                       show recent runs
    python -m puente reconcile --source SOURCE  account for every key in source, destination and review

SOURCE is a CSV export of the gold table, or the word databricks to read the gold
table from a Databricks SQL warehouse (settings in .env).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .client import SaaSClient
from .config import Settings
from .errors import AuthError, PuenteError
from .log import setup_logging
from .reconcile import Reconciliation, reconcile
from .retry import RetryPolicy
from .sources import CsvSource
from .state import REPLAYABLE, StateStore
from .sync import RunSummary, run_replay, run_sync
from .warehouse import DatabricksSource
from .warehouse import connect as connect_databricks


def make_source(name: str, settings: Settings) -> CsvSource | DatabricksSource:
    if name != "databricks":
        return CsvSource(name)
    missing = settings.missing_databricks_settings()
    if missing:
        raise PuenteError(f"set {', '.join(missing)} in .env to read from Databricks")
    return DatabricksSource(
        settings.databricks_table,
        connect=lambda: connect_databricks(settings.databricks_host, settings.databricks_http_path, settings.databricks_token),
    )


def build_client(settings: Settings) -> SaaSClient:
    policy = RetryPolicy(max_attempts=settings.max_attempts, max_elapsed_s=settings.max_elapsed_s)
    return SaaSClient(settings.saas_url, settings.client_id, settings.client_secret, timeout_s=settings.timeout_s, retry=policy)


def print_run(summary: RunSummary) -> None:
    print(f"{summary.kind} {summary.run_id}: {summary.status} in {summary.duration_s:.1f} s")
    for label, value in (
        ("rows in", summary.rows_in),
        ("lines ready to send", summary.lines_ready),
        ("delivered", summary.lines_delivered),
        ("already in destination", summary.already_in_destination),
        ("exact duplicates dropped", summary.exact_duplicates_dropped),
        ("parked for review", sum(summary.review_added.values())),
        ("resolved from review", summary.review_resolved),
        ("retries", summary.retries),
        ("circuit breaker opened", summary.breaker_opened),
    ):
        print(f"  {label:<28}{value:>8,}")
    for reason, count in sorted(summary.review_added.items()):
        print(f"      {reason:<24}{count:>8,}")


def print_review(store: StateStore, limit: int) -> None:
    counts = store.counts().get("open", {})
    if not counts:
        print("review queue is empty")
        return
    print(f"open review items: {sum(counts.values()):,}")
    for reason, count in sorted(counts.items(), key=lambda item: -item[1]):
        kind = "replayable" if reason in REPLAYABLE else "needs a person"
        print(f"  {reason:<26}{count:>7,}  {kind}")
    for item in store.open_items()[:limit]:
        print(f"  #{item['id']} row {item['source_row']} {item['external_id']}: {item['reason']}: {item['detail']}")


def print_runs(store: StateStore, limit: int = 10) -> None:
    for run in store.recent_runs(limit):
        summary = json.loads(run["summary_json"] or "{}")
        parked = sum((summary.get("review_added") or {}).values())
        print(f"{run['started_at']}  {run['kind']:<7} {run['status']:<10} delivered {summary.get('lines_delivered', 0):>7,}  parked {parked:>6,}  {run['run_id']}")


def print_reconciliation(result: Reconciliation) -> None:
    print(f"source: {result.source_rows:,} rows, {result.source_keys:,} business keys")
    for label, value in (
        ("delivered exactly once", result.delivered_once),
        ("waiting in review", result.in_review),
        ("lost", len(result.lost)),
        ("paid twice", len(result.paid_twice)),
        ("wrong amount", len(result.wrong_amount)),
        ("delivered and in review", len(result.delivered_and_in_review)),
        ("in destination, not in source", len(result.not_in_source)),
    ):
        print(f"  {label:<32}{value:>8,}")
    for label, keys in (("lost", result.lost), ("paid twice", result.paid_twice), ("wrong amount", result.wrong_amount)):
        if keys:
            print(f"  first {label}: {', '.join(keys[:5])}")
    print("clean" if result.clean else "NOT clean")


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="print info logs to the console")
    parser = argparse.ArgumentParser(prog="puente", description="Deliver warehouse rows to a SaaS API without losing or duplicating them.")
    commands = parser.add_subparsers(dest="command", required=True)
    sync = commands.add_parser("sync", parents=[common], help="deliver the gold table to the destination")
    sync.add_argument("--source", required=True, help="a CSV export, or databricks")
    sync.add_argument("--no-replay", action="store_true", help="skip the automatic replay of transient failures")
    commands.add_parser("replay", parents=[common], help="retry review items that failed for transient reasons")
    review = commands.add_parser("review", parents=[common], help="show the open review queue")
    review.add_argument("--limit", type=int, default=10)
    commands.add_parser("runs", parents=[common], help="show recent runs")
    check = commands.add_parser("reconcile", parents=[common], help="check source, destination and review queue against each other")
    check.add_argument("--source", required=True, help="a CSV export, or databricks")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    setup_logging(settings.log_file, console_level=logging.INFO if args.verbose else logging.WARNING)
    store = StateStore(settings.state_db)
    try:
        if args.command == "sync":
            source = make_source(args.source, settings)
            with build_client(settings) as client:
                summary = run_sync(client, store, source.rows(), source=source.describe(), batch_size=settings.batch_size, auto_replay=not args.no_replay)
            print_run(summary)
            if summary.replay:
                print_run(summary.replay)
        elif args.command == "replay":
            with build_client(settings) as client:
                print_run(run_replay(client, store, batch_size=settings.batch_size))
        elif args.command == "review":
            print_review(store, args.limit)
        elif args.command == "runs":
            print_runs(store)
        elif args.command == "reconcile":
            rows = list(make_source(args.source, settings).rows())
            with build_client(settings) as client:
                result = reconcile(rows, client.iter_payouts(), store.open_items())
            print_reconciliation(result)
            return 0 if result.clean else 3
    except AuthError as exc:
        print(f"credentials refused: {exc}", file=sys.stderr)
        return 2
    except PuenteError as exc:
        print(f"run failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # a driver or network error from the source, for example
        print(f"run failed: {type(exc).__name__}: {exc} (details in {settings.log_file})", file=sys.stderr)
        return 1
    finally:
        store.close()
    return 0
