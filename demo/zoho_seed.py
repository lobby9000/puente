"""Stage 3 data: the sales network in Zoho CRM, the way Sales Ops keeps it.

Stores become Accounts, matched on Store_Code, and sales reps become Contacts,
matched on Vendor_Code and linked to their store, with the payout account the
team captured for them. Both go in with upsert, so a second run changes nothing.

The CRM brings defects of its own, each on a rep whose payouts were clean in
stage 2, so every business key still carries at most one problem:

* reps that Sales Ops put on hold (Payout_Hold checked)
* a rep whose Payee_ID was copied from another rep's record: the payouts tool
  would accept the line and pay the wrong person
* a rep whose Payee_ID has a typo, so it matches no account
* a rep missing from the CRM
* the new reps of stage 2, whose payout account is not open yet (no Payee_ID)

    python -m demo.zoho_seed --raw data/raw            load Zoho, write the expected outcome
    python -m demo.zoho_seed --raw data/raw --check    compare the review queue with it

Run `python -m puente zoho setup` first, so the custom fields exist.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from demo.generate_raw import PLAN_FILE, TRUTH_FILE, read_csv
from mock_saas.seed import country_for, payee_id, vendor_code
from puente.config import Settings
from puente.errors import PuenteError
from puente.zoho import UpsertResult, ZohoClient
from puente.zoho_setup import check_fields

CRM_TRUTH_FILE = "crm_truth.json"
HOLD_REPS = 2
COPIED_PAYEE_REPS = 1
TYPO_PAYEE_REPS = 1
MISSING_REPS = 1
COUNTRY_NAMES = {"MX": "Mexico", "CO": "Colombia", "PE": "Peru", "BR": "Brazil"}


@dataclass
class CrmData:
    accounts: list[dict]
    contacts: list[dict]
    store_of: dict[str, str]
    defects: dict[str, list[str]]
    expected: dict[str, str]  # business key -> outcome once payees come from Zoho
    copied_from: dict[str, str] = field(default_factory=dict)


def build_crm(plan: list[dict], truth_keys: dict[str, str], vendors: int, seed: int = 3) -> CrmData:
    rng = random.Random(seed)
    store_of: dict[str, str] = {}
    for row in sorted(plan, key=lambda row: row["vendor_code"]):
        store_of.setdefault(row["vendor_code"], row["store_id"])
    accounts = [
        {"Account_Name": f"Store {store}", "Store_Code": store, "Billing_Country": COUNTRY_NAMES.get(store[:2], store[:2])}
        for store in sorted(set(store_of.values()))
    ]

    keys_of: dict[str, list[str]] = defaultdict(list)
    for key in truth_keys:
        keys_of[key.split(":", 1)[1]].append(key)
    regular = [vendor_code(i) for i in range(1, vendors + 1)]
    index_of = {code: i for i, code in enumerate(regular, start=1)}
    new_reps = sorted(code for code in keys_of if code not in index_of)
    clean = [code for code in regular if keys_of[code] and all(truth_keys[key] == "paid" for key in keys_of[code])]
    rng.shuffle(clean)
    hold = [clean.pop() for _ in range(HOLD_REPS)]
    copied = [clean.pop() for _ in range(COPIED_PAYEE_REPS)]
    typo = [clean.pop() for _ in range(TYPO_PAYEE_REPS)]
    missing = [clean.pop() for _ in range(MISSING_REPS)]
    # Copy the account of a rep in the same country: same currency, so nothing downstream would object.
    copied_from = {code: next(other for other in clean if country_for(index_of[other]) == country_for(index_of[code])) for code in copied}

    contacts = []
    for code in regular:
        if code in missing:
            continue
        account = payee_id(index_of[code])
        if code in copied:
            account = payee_id(index_of[copied_from[code]])
        elif code in typo:
            account = account.replace("PY-", "PY-0")  # one zero too many
        contacts.append({"Last_Name": f"Rep {code}", "Vendor_Code": code, "Payee_ID": account, "Payout_Hold": code in hold})
    for code in new_reps:
        contacts.append({"Last_Name": f"Rep {code}", "Vendor_Code": code, "Payee_ID": None, "Payout_Hold": False})

    expected = dict(truth_keys)
    for codes, outcome in (
        (hold, "review:payout_on_hold"),
        (copied, "review:payee_mismatch"),
        (typo + missing + new_reps, "review:unmapped_payee"),
    ):
        for code in codes:
            for key in keys_of[code]:
                expected[key] = outcome
    defects = {"payout_hold": hold, "copied_payee_id": copied, "payee_id_typo": typo, "not_in_crm": missing, "no_payee_id_yet": new_reps}
    return CrmData(accounts, contacts, store_of, defects, expected, copied_from)


@dataclass
class SeedReport:
    accounts: list[UpsertResult]
    contacts: list[UpsertResult]

    @staticmethod
    def tally(results: list[UpsertResult]) -> Counter[str]:
        return Counter(result.action if result.ok else f"failed:{result.code}" for result in results)


def seed_crm(zoho: ZohoClient, crm: CrmData) -> SeedReport:
    """Upsert the stores, then the reps linked to the store records Zoho just confirmed."""
    accounts = zoho.upsert("Accounts", crm.accounts, key_field="Store_Code")
    account_ids = {result.key: result.record_id for result in accounts if result.ok and result.record_id}
    contacts = []
    for contact in crm.contacts:
        record = dict(contact)
        store = crm.store_of.get(record["Vendor_Code"])
        if store in account_ids:
            record["Account_Name"] = {"id": account_ids[store]}
        contacts.append(record)
    return SeedReport(accounts, zoho.upsert("Contacts", contacts, key_field="Vendor_Code"))


def check_outcomes(expected: dict[str, str], open_items: list[dict]) -> tuple[Counter[str], list[tuple[str, str, str]]]:
    """Compare the expected outcome of each key with the open review queue."""
    parked = {item["external_id"]: item["reason"] for item in open_items}
    matched: Counter[str] = Counter()
    wrong = []
    for key, outcome in sorted(expected.items()):
        want = outcome.split(":", 1)[1] if outcome.startswith("review:") else None
        got = parked.get(key)
        if got == want:
            matched[outcome] += 1
        else:
            wrong.append((key, outcome, f"review:{got}" if got else "not in review"))
    return matched, wrong


def load_inputs(raw: Path) -> tuple[list[dict], dict]:
    plan_path, truth_path = raw / PLAN_FILE, raw / TRUTH_FILE
    if not plan_path.is_file() or not truth_path.is_file():
        raise PuenteError(f"{plan_path} and {truth_path} are missing: run python -m demo.generate_raw --out {raw} (or demo.databricks_setup) first")
    return read_csv(plan_path), json.loads(truth_path.read_text(encoding="utf-8"))


def run_check(raw: Path, settings: Settings) -> int:
    from puente.state import StateStore

    truth_path = raw / CRM_TRUTH_FILE
    if not truth_path.is_file():
        raise PuenteError(f"{truth_path} is missing: load Zoho with python -m demo.zoho_seed --raw {raw} first")
    expected = json.loads(truth_path.read_text(encoding="utf-8"))["keys"]
    store = StateStore(settings.state_db)
    try:
        matched, wrong = check_outcomes(expected, store.open_items())
    finally:
        store.close()
    print(f"business keys: {len(expected):,}; as expected: {sum(matched.values()):,}")
    for outcome, count in sorted(matched.items()):
        print(f"  {outcome:<36}{count:>7,}")
    for key, want, got in wrong[:10]:
        print(f"  {key}: expected {want}, found {got}")
    if wrong:
        print(f"{len(wrong):,} keys differ (paid keys are confirmed by puente reconcile)")
        return 3
    print("every key is where it should be (run puente reconcile to confirm the payouts)")
    return 0


def main(argv: list[str] | None = None) -> int:
    from puente.cli import build_zoho

    parser = argparse.ArgumentParser(description="Load the stage 3 sales network into Zoho CRM, or check a run against it.")
    parser.add_argument("--raw", default="data/raw", help="the folder demo.generate_raw wrote")
    parser.add_argument("--seed", type=int, default=3)
    parser.add_argument("--check", action="store_true", help="compare the review queue (PUENTE_STATE_DB) with the expected outcome")
    args = parser.parse_args(argv)
    raw = Path(args.raw)
    settings = Settings.from_env()
    try:
        if args.check:
            return run_check(raw, settings)
        plan, truth = load_inputs(raw)
        crm = build_crm(plan, truth["keys"], truth["vendors"], args.seed)
        with build_zoho(settings) as zoho:
            not_ready = [check for check in check_fields(zoho) if not check.usable]
            if not_ready:
                names = ", ".join(f"{check.spec.module}.{check.spec.api_name}" for check in not_ready)
                raise PuenteError(f"Zoho lacks {names}: run python -m puente zoho setup first")
            report = seed_crm(zoho, crm)
            used, budget = zoho.credits.used, zoho.credits.budget
    except PuenteError as exc:
        print(f"failed: {exc}", file=sys.stderr)
        return 1

    for label, results in (("accounts (stores)", report.accounts), ("contacts (reps)", report.contacts)):
        counts = SeedReport.tally(results)
        print(f"{label:<20}" + ", ".join(f"{name} {count:,}" for name, count in sorted(counts.items())))
    for result in [r for r in report.accounts + report.contacts if not r.ok][:5]:
        print(f"  {result.key}: {result.code} {result.field or ''} {result.message}")
    print(f"zoho credits used: {used:,} of a budget of {budget:,}")
    print("defects in the CRM:")
    for name, codes in crm.defects.items():
        shown = ", ".join(codes[:4]) + (" ..." if len(codes) > 4 else "")
        print(f"  {name:<18}{len(codes):>3}  {shown}")
    for code, other in crm.copied_from.items():
        print(f"  {code} carries the Payee_ID of {other}")
    expected_counts = dict(sorted(Counter(crm.expected.values()).items()))
    (raw / CRM_TRUTH_FILE).write_text(json.dumps({"defects": crm.defects, "copied_from": crm.copied_from, "expected": expected_counts, "keys": crm.expected}, indent=1), encoding="utf-8")
    print("expected outcome with --payees zoho:")
    for outcome, count in expected_counts.items():
        print(f"  {outcome:<36}{count:>7,}")
    return 0 if all(r.ok for r in report.accounts + report.contacts) else 1


if __name__ == "__main__":
    raise SystemExit(main())
