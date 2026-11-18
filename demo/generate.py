"""Synthetic data in the shape of a point-of-sale phone financing business.

Each sales rep sells phones on credit at one store in Mexico, Colombia, Peru or
Brazil and earns a monthly commission. The output is the gold table a warehouse
would hand to the integration, with defects injected at known rates, plus a
ground-truth file that says what should happen to every business key.

    python -m demo.generate --vendors 850 --months 12 --out data
"""

from __future__ import annotations

import argparse
import csv
import json
import random
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from mock_saas.models import PAYOUT_CAPS
from mock_saas.seed import CURRENCY_BY_COUNTRY, country_for, vendor_code

SOURCE_FILE = "gold_vendor_commissions.csv"
TRUTH_FILE = "ground_truth.json"
COLUMNS = ("vendor_code", "store_id", "country", "currency", "period", "financed_sales", "financed_amount", "commission_rate", "commission_amount")

CENT = Decimal("0.01")
# Typical financed phone price in local currency, about 200 USD.
TICKET = {"MX": Decimal("3600"), "CO": Decimal("780000"), "PE": Decimal("690"), "BR": Decimal("1100")}
RATES = (Decimal("0.025"), Decimal("0.030"), Decimal("0.035"), Decimal("0.040"))

# Share of rows that get each defect.
SHARE_INVALID = 0.005  # zero or negative amount, unknown or wrong currency, bad period, empty amount
SHARE_OVER_CAP = 0.002  # amount inflated past the destination's per-line cap (a misplaced decimal)
SHARE_EXACT_DUPLICATE = 0.002  # the same row exported twice
SHARE_CONFLICTING_DUPLICATE = 0.001  # the same rep and month twice, with different amounts
SHARE_UNMAPPED = 0.01  # rows for new reps who do not exist in the destination yet


def last_months(count: int, last: str = "2026-10") -> list[str]:
    year, month = (int(part) for part in last.split("-"))
    months = []
    for _ in range(count):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return months[::-1]


def business_key(row: dict) -> str:
    return f"{row['period']}:{row['vendor_code']}"


def commission_row(rng: random.Random, code: str, country: str, store: str, rate: Decimal, period: str) -> dict:
    sales = max(1, round(rng.gauss(28, 10)))
    spread = Decimal(str(round(rng.uniform(0.85, 1.25), 3)))
    financed = (TICKET[country] * sales * spread).quantize(CENT, ROUND_HALF_UP)
    return {
        "vendor_code": code,
        "store_id": store,
        "country": country,
        "currency": CURRENCY_BY_COUNTRY[country],
        "period": period,
        "financed_sales": str(sales),
        "financed_amount": str(financed),
        "commission_rate": str(rate),
        "commission_amount": str((financed * rate).quantize(CENT, ROUND_HALF_UP)),
    }


def generate(vendors: int, months: int, seed: int, out_dir: str | Path) -> dict:
    rng = random.Random(seed)
    periods = last_months(months)
    rows: list[dict] = []
    for i in range(1, vendors + 1):
        country = country_for(i)
        store = f"{country}-T{rng.randint(1, 40):03d}"
        rate = rng.choice(RATES)
        rows.extend(commission_row(rng, vendor_code(i), country, store, rate, period) for period in periods)

    truth = {business_key(row): "paid" for row in rows}
    pool = list(range(len(rows)))
    rng.shuffle(pool)

    def take(share: float) -> list[int]:
        return [pool.pop() for _ in range(max(1, round(len(rows) * share)))]

    for index in take(SHARE_INVALID):
        row = rows[index]
        old_key = business_key(row)
        defect = rng.choice(("zero_amount", "negative_amount", "unknown_currency", "wrong_currency", "bad_period", "empty_amount"))
        if defect == "zero_amount":
            row["commission_amount"] = "0.00"
        elif defect == "negative_amount":
            row["commission_amount"] = f"-{row['commission_amount']}"
        elif defect == "unknown_currency":
            row["currency"] = "USD"
        elif defect == "wrong_currency":
            row["currency"] = "COP" if row["currency"] == "MXN" else "MXN"
        elif defect == "bad_period":
            row["period"] = row["period"].replace("-", "/")
        else:
            row["commission_amount"] = ""
        del truth[old_key]
        truth[business_key(row)] = "review:invalid_contract"

    for index in take(SHARE_OVER_CAP):
        row = rows[index]
        inflated = Decimal(row["commission_amount"]) * 100
        while inflated <= PAYOUT_CAPS[row["currency"]]:
            inflated *= 10
        row["commission_amount"] = str(inflated.quantize(CENT))
        truth[business_key(row)] = "review:rejected_by_destination"

    extra: list[dict] = []
    exact_duplicates = 0
    for index in take(SHARE_EXACT_DUPLICATE):
        extra.append(dict(rows[index]))
        exact_duplicates += 1

    for index in take(SHARE_CONFLICTING_DUPLICATE):
        twin = dict(rows[index])
        twin["commission_amount"] = str((Decimal(twin["commission_amount"]) + Decimal("150")).quantize(CENT))
        extra.append(twin)
        truth[business_key(twin)] = "review:conflicting_duplicate"

    for j in range(max(1, round(len(rows) * SHARE_UNMAPPED / months))):
        country = rng.choice(("MX", "CO", "PE", "BR"))
        store = f"{country}-T{rng.randint(1, 40):03d}"
        rate = rng.choice(RATES)
        for period in periods:
            row = commission_row(rng, f"V-{90001 + j}", country, store, rate, period)
            extra.append(row)
            truth[business_key(row)] = "review:unmapped_payee"

    table = rows + extra
    rng.shuffle(table)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    with (out / SOURCE_FILE).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(table)

    summary = {
        "seed": seed,
        "vendors": vendors,
        "months": months,
        "rows": len(table),
        "key_count": len(truth),
        "exact_duplicates": exact_duplicates,
        "expected": dict(sorted(Counter(truth.values()).items())),
    }
    (out / TRUTH_FILE).write_text(json.dumps({**summary, "keys": truth}, indent=1), encoding="utf-8")
    return {**summary, "keys": truth}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic commission data with known defects.")
    parser.add_argument("--vendors", type=int, default=850)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="data")
    args = parser.parse_args(argv)
    result = generate(args.vendors, args.months, args.seed, args.out)
    print(f"{result['rows']:,} rows, {result['key_count']:,} business keys -> {Path(args.out) / SOURCE_FILE}")
    for outcome, count in result["expected"].items():
        print(f"  {outcome:<36}{count:>7,}")
    print(f"  {'exact duplicate rows':<36}{result['exact_duplicates']:>7,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
