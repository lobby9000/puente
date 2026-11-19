"""Command line.

    python -m puente sync --source SOURCE       deliver rows, then replay transient failures
    python -m puente replay                     retry review items that failed for transient reasons
    python -m puente review                     show the open review queue
    python -m puente runs                       show recent runs
    python -m puente reconcile --source SOURCE  account for every key in source, destination and review
    python -m puente zoho auth                  trade a Zoho Self Client grant code for a refresh token
    python -m puente zoho check                 show the Zoho org and whether Puente's fields exist
    python -m puente zoho setup                 create the custom fields Puente needs in Zoho

SOURCE is a CSV export of the gold table, or the word databricks to read the gold
table from a Databricks SQL warehouse (settings in .env). sync --payees zoho maps
sales reps to payees with the contacts in Zoho CRM instead of the destination.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
from collections.abc import Callable

from .client import SaaSClient
from .config import Settings, set_env_values
from .errors import AuthError, PuenteError
from .log import setup_logging
from .payees import CONTACT_FIELDS, PayeeDirectory, from_zoho
from .reconcile import Reconciliation, reconcile
from .retry import RetryPolicy
from .sources import CsvSource
from .state import REPLAYABLE, StateStore
from .sync import RunSummary, run_replay, run_sync
from .warehouse import DatabricksSource
from .warehouse import connect as connect_databricks
from .zoho import SCOPES, CreditMeter, ZohoClient
from .zoho_setup import FieldCheck, check_fields, ensure_fields


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


def build_zoho(settings: Settings, *, need_refresh_token: bool = True) -> ZohoClient:
    missing = settings.missing_zoho_settings(need_refresh_token)
    if missing:
        raise PuenteError(f"set {', '.join(missing)} in .env to reach Zoho CRM")
    return ZohoClient(
        settings.zoho_client_id,
        settings.zoho_client_secret,
        settings.zoho_refresh_token,
        accounts_url=settings.zoho_accounts_url,
        api_domain=settings.zoho_api_domain,
        timeout_s=settings.zoho_timeout_s,
        retry=RetryPolicy(max_attempts=settings.max_attempts, max_elapsed_s=settings.max_elapsed_s),
        credits=CreditMeter(budget=settings.zoho_credit_budget, reserve=settings.zoho_credit_reserve),
    )


def zoho_payees(zoho: ZohoClient, client: SaaSClient) -> Callable[[], PayeeDirectory]:
    return lambda: from_zoho(zoho.iter_records("Contacts", CONTACT_FIELDS), client.list_payees())


def print_credits(zoho: ZohoClient) -> None:
    left = f", {zoho.credits.remaining:,} left for the org today" if zoho.credits.remaining is not None else ""
    print(f"zoho credits used: {zoho.credits.used:,} of a budget of {zoho.credits.budget:,}{left}")


def print_fields(checks: list[FieldCheck]) -> None:
    for check in checks:
        spec = check.spec
        note = f"  {check.detail}" if check.detail else ""
        print(f"  {spec.module + '.' + spec.api_name:<22}{check.status:<9}{note}")


def zoho_auth(settings: Settings, env_path: str = ".env", read_code: Callable[[str], str] = getpass.getpass) -> None:
    if settings.missing_zoho_settings(need_refresh_token=False):
        raise PuenteError("set ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET in .env first (api-console.zoho.com > Self Client > Client Secret)")
    print("In api-console.zoho.com open your Self Client, tab Generate Code, and fill in:")
    print(f"  Scope          {','.join(SCOPES)}")
    print("  Time Duration  10 minutes")
    print("  Description    Puente")
    print("Press Create, choose your Developer Edition org and copy the code.")
    code = read_code("Paste the code here (it is not shown): ").strip()
    if not code:
        raise PuenteError("no code given")
    with build_zoho(settings, need_refresh_token=False) as zoho:
        body = zoho.exchange_grant_code(code)
        api_domain = zoho.api_domain
    set_env_values(env_path, {"ZOHO_REFRESH_TOKEN": body["refresh_token"], "ZOHO_API_DOMAIN": api_domain})
    print(f"saved ZOHO_REFRESH_TOKEN (not shown) and ZOHO_API_DOMAIN={api_domain} in {env_path}")
    if body.get("scope"):
        print(f"scopes granted: {body['scope']}")


def zoho_fields(settings: Settings, create: bool) -> int:
    with build_zoho(settings) as zoho:
        org = zoho.get_org()
        print(f"org: {org.get('company_name') or org.get('id') or '(no name)'} at {zoho.api_domain}")
        checks = ensure_fields(zoho) if create else check_fields(zoho)
        print_fields(checks)
        print_credits(zoho)
    if all(check.usable for check in checks):
        print("ready")
        return 0
    print("run python -m puente zoho setup" if not create else "some fields could not be created, see above")
    return 4


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
    if summary.payee_source != "destination":
        stats = summary.payee_stats
        print(f"  payees from {summary.payee_source}: {stats.get('contacts_read', 0):,} contacts read, {stats.get('usable', 0):,} reps can be paid")
        for key, count in stats.items():
            if key.startswith("blocked:"):
                print(f"      {key[len('blocked:'):]:<24}{count:>8,} reps")


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
    sync.add_argument("--payees", choices=("destination", "zoho"), help="where rep-to-payee mappings come from (default: PUENTE_PAYEES or destination)")
    commands.add_parser("replay", parents=[common], help="retry review items that failed for transient reasons")
    review = commands.add_parser("review", parents=[common], help="show the open review queue")
    review.add_argument("--limit", type=int, default=10)
    commands.add_parser("runs", parents=[common], help="show recent runs")
    check = commands.add_parser("reconcile", parents=[common], help="check source, destination and review queue against each other")
    check.add_argument("--source", required=True, help="a CSV export, or databricks")
    zoho = commands.add_parser("zoho", help="connect to Zoho CRM (stage 3)")
    zoho_commands = zoho.add_subparsers(dest="zoho_command", required=True)
    zoho_commands.add_parser("auth", parents=[common], help="trade a Self Client grant code for a refresh token, saved in .env")
    zoho_commands.add_parser("check", parents=[common], help="show the org and whether Puente's fields exist (3 credits)")
    zoho_commands.add_parser("setup", parents=[common], help="create the missing fields (10 credits per field)")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    setup_logging(settings.log_file, console_level=logging.INFO if args.verbose else logging.WARNING)
    store = StateStore(settings.state_db)
    try:
        if args.command == "sync":
            source = make_source(args.source, settings)
            payee_source = args.payees or settings.payees
            zoho = build_zoho(settings) if payee_source == "zoho" else None
            try:
                with build_client(settings) as client:
                    summary = run_sync(
                        client,
                        store,
                        source.rows(),
                        source=source.describe(),
                        batch_size=settings.batch_size,
                        auto_replay=not args.no_replay,
                        payees=zoho_payees(zoho, client) if zoho else None,
                    )
            finally:
                if zoho:
                    zoho.close()
            print_run(summary)
            if summary.replay:
                print_run(summary.replay)
            if zoho:
                print_credits(zoho)
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
        elif args.command == "zoho":
            if args.zoho_command == "auth":
                zoho_auth(settings)
            else:
                return zoho_fields(settings, create=args.zoho_command == "setup")
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
