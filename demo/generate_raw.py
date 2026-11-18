"""Stage 2 data: raw sales events and a commission plan, as a warehouse receives them.

The SQL in warehouse/ turns these two files into the gold table Puente delivers.
This time the defects start upstream, and each one reaches gold the way it would
in real life:

* a sale exported twice: silver keeps one copy, so gold is unaffected
* a rep listed twice in the plan with two rates: two gold rows for one key
* a rep with the wrong currency, or with one the destination does not pay in
* sales by a rep missing from the plan: no rate, so no commission
* refunds larger than the month's sales: a commission at or below zero
* a store that imported one month in cents: amounts 100 times too big
* new reps who do not exist in the payouts tool yet

    python -m demo.generate_raw --out data/raw
"""

from __future__ import annotations

import argparse
import calendar
import csv
import json
import random
from collections import Counter
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from demo.generate import COLUMNS as GOLD_COLUMNS
from demo.generate import RATES, TICKET, last_months
from mock_saas.models import PAYOUT_CAPS
from mock_saas.seed import CURRENCY_BY_COUNTRY, country_for, vendor_code

SALES_FILE = "sales_raw.csv"
PLAN_FILE = "comp_plan.csv"
GOLD_FILE = "gold_reference.csv"
TRUTH_FILE = "ground_truth.json"
SALES_COLUMNS = ("sale_id", "sale_date", "vendor_code", "financed_amount")
PLAN_COLUMNS = ("vendor_code", "store_id", "country", "currency", "commission_rate")
CENT = Decimal("0.01")

# Each defect lands on its own reps or months, so a business key carries at most
# one problem and its expected outcome is never ambiguous.
CONFLICTING_PLAN_REPS = 2
WRONG_CURRENCY_REPS = 2
UNKNOWN_CURRENCY_REPS = 1
MISSING_PLAN_REPS = 1
SHARE_CLAWBACK_MONTHS = 0.0015
SHARE_CENTS_MONTHS = 0.002
SHARE_NEW_REPS = 0.01
SHARE_DUPLICATE_SALES = 0.003
SHARE_MONTHS_WITH_A_REFUND = 0.3


def money(value: Decimal) -> Decimal:
    return value.quantize(CENT, ROUND_HALF_UP)


def write_csv(path: Path, columns: Iterable[str], rows: Iterable[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns))
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: str | Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def reference_gold(sales: list[dict], plan: list[dict]) -> list[dict]:
    """The gold table computed in Python, exactly as warehouse/02_silver.sql and 03_gold.sql do it."""
    seen: set[str] = set()
    silver = []
    for event in sales:  # one row per sale_id
        if event["sale_id"] not in seen:
            seen.add(event["sale_id"])
            silver.append(event)
    plans: dict[str, list[dict]] = {}
    for row in plan:
        plans.setdefault(row["vendor_code"], []).append(row)

    groups: dict[tuple, list] = {}
    for event in silver:
        amount = Decimal(event["financed_amount"])
        for row in plans.get(event["vendor_code"]) or [None]:  # LEFT JOIN
            rate = Decimal(row["commission_rate"]) if row and row["commission_rate"] else None
            key = (
                event["vendor_code"],
                row["store_id"] if row else None,
                row["country"] if row else None,
                row["currency"] if row else None,
                event["sale_date"][:7],
                rate,
            )
            totals = groups.setdefault(key, [0, Decimal("0.00")])
            totals[0] += amount > 0
            totals[1] += amount

    gold = []
    for (code, store, country, currency, period, rate), (positive, total) in groups.items():
        commission = money(total * rate) if rate is not None else None
        gold.append(
            {
                "vendor_code": code,
                "store_id": store or "",
                "country": country or "",
                "currency": currency or "",
                "period": period,
                "financed_sales": str(positive),
                "financed_amount": f"{total:.2f}",
                "commission_rate": f"{rate:.4f}" if rate is not None else "",
                "commission_amount": f"{commission:.2f}" if commission is not None else "",
            }
        )
    gold.sort(key=lambda row: (row["period"], row["vendor_code"], row["commission_rate"]))
    return gold


def generate_raw(vendors: int, months: int, seed: int, out_dir: str | Path) -> dict:
    rng = random.Random(seed)
    periods = last_months(months)

    plan: list[dict] = []
    country_of: dict[str, str] = {}

    def plan_row(code: str, country: str) -> dict:
        country_of[code] = country
        return {
            "vendor_code": code,
            "store_id": f"{country}-T{rng.randint(1, 40):03d}",
            "country": country,
            "currency": CURRENCY_BY_COUNTRY[country],
            "commission_rate": str(rng.choice(RATES)),
        }

    regular = [vendor_code(i) for i in range(1, vendors + 1)]
    plan.extend(plan_row(code, country_for(i)) for i, code in enumerate(regular, start=1))
    new_reps = [f"V-{90001 + j}" for j in range(max(1, round(vendors * SHARE_NEW_REPS)))]
    plan.extend(plan_row(code, rng.choice(("MX", "CO", "PE", "BR"))) for code in new_reps)

    pool = regular[:]
    rng.shuffle(pool)
    conflicting = [pool.pop() for _ in range(CONFLICTING_PLAN_REPS)]
    wrong_currency = [pool.pop() for _ in range(WRONG_CURRENCY_REPS)]
    unknown_currency = [pool.pop() for _ in range(UNKNOWN_CURRENCY_REPS)]
    missing_plan = [pool.pop() for _ in range(MISSING_PLAN_REPS)]
    clean_reps = pool

    entry = {row["vendor_code"]: row for row in plan}
    for code in conflicting:
        current = Decimal(entry[code]["commission_rate"])
        plan.append({**entry[code], "commission_rate": str(next(rate for rate in RATES if rate != current))})
    for code in wrong_currency:
        entry[code]["currency"] = "COP" if entry[code]["currency"] == "MXN" else "MXN"
    for code in unknown_currency:
        entry[code]["currency"] = "USD"
    plan = [row for row in plan if row["vendor_code"] not in set(missing_plan)]

    next_id = 0

    def sale(code: str, period: str, amount: Decimal) -> dict:
        nonlocal next_id
        next_id += 1
        day = rng.randint(1, calendar.monthrange(int(period[:4]), int(period[5:]))[1])
        return {"sale_id": f"S-{next_id:07d}", "sale_date": f"{period}-{day:02d}", "vendor_code": code, "financed_amount": str(amount)}

    sales_by_key: dict[tuple[str, str], list[dict]] = {}
    for code in regular + new_reps:
        ticket = TICKET[country_of[code]]
        for period in periods:
            count = max(1, round(rng.gauss(28, 10)))
            events = [sale(code, period, money(ticket * Decimal(str(round(rng.uniform(0.6, 1.5), 3))))) for _ in range(count)]
            if count >= 3 and rng.random() < SHARE_MONTHS_WITH_A_REFUND:
                events.append(sale(code, period, -Decimal(rng.choice(events)["financed_amount"])))
            sales_by_key[(code, period)] = events

    def net(key: tuple[str, str]) -> Decimal:
        return sum((Decimal(event["financed_amount"]) for event in sales_by_key[key]), Decimal("0.00"))

    rate_of = {row["vendor_code"]: Decimal(row["commission_rate"]) for row in plan if row["vendor_code"] in set(clean_reps)}
    candidates = [(code, period) for code in clean_reps for period in periods]
    rng.shuffle(candidates)
    clawback_keys = candidates[: max(1, round(len(regular) * months * SHARE_CLAWBACK_MONTHS))]
    cents_keys: list[tuple[str, str]] = []
    for key in candidates[len(clawback_keys) :]:
        if len(cents_keys) >= max(1, round(len(regular) * months * SHARE_CENTS_MONTHS)):
            break
        cap = PAYOUT_CAPS[CURRENCY_BY_COUNTRY[country_of[key[0]]]]
        if money(net(key) * 100 * rate_of[key[0]]) > cap * Decimal("1.2"):
            cents_keys.append(key)

    for code, period in clawback_keys:
        # Phones financed in earlier months were cancelled now, and the refunds outweigh this month's sales.
        refund = -(net((code, period)) + money(TICKET[country_of[code]] / 2))
        sales_by_key[(code, period)].append(sale(code, period, refund))
    for key in cents_keys:
        for event in sales_by_key[key]:
            event["financed_amount"] = str(Decimal(event["financed_amount"]) * 100)

    sales = [event for events in sales_by_key.values() for event in events]
    duplicates = [dict(event) for event in rng.sample(sales, max(1, round(len(sales) * SHARE_DUPLICATE_SALES)))]
    sales.extend(duplicates)
    rng.shuffle(sales)
    rng.shuffle(plan)

    truth = {f"{period}:{code}": "paid" for code in regular + new_reps for period in periods}
    for code, outcome in (
        *((code, "review:conflicting_duplicate") for code in conflicting),
        *((code, "review:invalid_contract") for code in wrong_currency + unknown_currency + missing_plan),
        *((code, "review:unmapped_payee") for code in new_reps),
    ):
        for period in periods:
            truth[f"{period}:{code}"] = outcome
    for code, period in clawback_keys:
        truth[f"{period}:{code}"] = "review:invalid_contract"
    for code, period in cents_keys:
        truth[f"{period}:{code}"] = "review:rejected_by_destination"

    gold = reference_gold(sales, plan)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / SALES_FILE, SALES_COLUMNS, sales)
    write_csv(out / PLAN_FILE, PLAN_COLUMNS, plan)
    write_csv(out / GOLD_FILE, GOLD_COLUMNS, gold)
    summary = {
        "seed": seed,
        "vendors": vendors,
        "months": months,
        "sale_rows": len(sales),
        "duplicate_sale_rows": len(duplicates),
        "plan_rows": len(plan),
        "gold_rows": len(gold),
        "key_count": len(truth),
        "expected": dict(sorted(Counter(truth.values()).items())),
    }
    (out / TRUTH_FILE).write_text(json.dumps({**summary, "keys": truth}, indent=1), encoding="utf-8")
    return {**summary, "keys": truth, "gold": gold}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate raw sales events and a commission plan with upstream defects.")
    parser.add_argument("--vendors", type=int, default=850)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="data/raw")
    args = parser.parse_args(argv)
    result = generate_raw(args.vendors, args.months, args.seed, args.out)
    print(f"{result['sale_rows']:,} sale rows ({result['duplicate_sale_rows']:,} repeated), {result['plan_rows']:,} plan rows -> {args.out}")
    print(f"gold: {result['gold_rows']:,} rows for {result['key_count']:,} business keys")
    for outcome, count in result["expected"].items():
        print(f"  {outcome:<36}{count:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
