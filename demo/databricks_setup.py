"""Stage 2 setup: build bronze, silver and gold in a Databricks workspace (Free Edition works).

    python -m demo.databricks_setup

Reads DATABRICKS_HOST, DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN from the environment
or .env. It generates the raw files, uploads them to a Unity Catalog volume, runs
warehouse/*.sql on the SQL warehouse and checks that gold in Databricks matches the
local reference row by row.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from demo.generate import COLUMNS
from demo.generate_raw import PLAN_FILE, SALES_FILE, generate_raw
from puente.config import Settings
from puente.warehouse import DatabricksSource, connect, run_sql_file, upload_to_volume

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "warehouse"


def count_rows(connection, table: str) -> int:
    cursor = connection.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return int(cursor.fetchall()[0][0])
    finally:
        cursor.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the stage 2 tables in Databricks and check gold against the local reference.")
    parser.add_argument("--vendors", type=int, default=850)
    parser.add_argument("--months", type=int, default=12)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--out", default="data/raw")
    parser.add_argument("--catalog", default="workspace")
    parser.add_argument("--schema", default="puente")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    missing = settings.missing_databricks_settings()
    if missing:
        print(f"missing in .env: {', '.join(missing)} (see the Databricks section of the README)", file=sys.stderr)
        return 2
    target = f"{args.catalog}.{args.schema}"
    volume = f"/Volumes/{args.catalog}/{args.schema}/raw"
    replacements = {"target": target, "volume": volume}

    def open_connection():
        return connect(settings.databricks_host, settings.databricks_http_path, settings.databricks_token)

    print("1/5 generating the raw files")
    data = generate_raw(args.vendors, args.months, args.seed, args.out)
    print(f"    {data['sale_rows']:,} sale rows ({data['duplicate_sale_rows']:,} repeated) and {data['plan_rows']:,} plan rows in {args.out}")

    connection = open_connection()
    try:
        print(f"2/5 creating the schema {target} and its volume")
        run_sql_file(connection, SQL_DIR / "00_setup.sql", replacements)
        print("3/5 uploading the files to the volume")
        for name in (SALES_FILE, PLAN_FILE):
            upload_to_volume(settings.databricks_host, settings.databricks_token, Path(args.out) / name, f"{volume}/{name}")
            print(f"    {volume}/{name}")
        print("4/5 building bronze, silver and gold on the SQL warehouse")
        for name in ("01_bronze.sql", "02_silver.sql", "03_gold.sql"):
            run_sql_file(connection, SQL_DIR / name, replacements)
            print(f"    {name}")
        bronze = count_rows(connection, f"{target}.bronze_sales")
        silver = count_rows(connection, f"{target}.silver_sales")
    finally:
        connection.close()
    print(f"    bronze_sales {bronze:,} rows, silver_sales {silver:,} rows ({bronze - silver:,} repeated sales removed)")

    print("5/5 checking gold in Databricks against the local reference")
    table = f"{target}.gold_vendor_commissions"
    remote = sorted(tuple(row.data.get(column, "") for column in COLUMNS) for row in DatabricksSource(table, open_connection).rows())
    local = sorted(tuple(row[column] for column in COLUMNS) for row in data["gold"])
    if remote != local:
        only_remote, only_local = sorted(set(remote) - set(local)), sorted(set(local) - set(remote))
        print(f"    gold differs: {len(only_remote)} rows only in Databricks, {len(only_local)} only in the reference", file=sys.stderr)
        for row in (only_remote[:3] + only_local[:3]):
            print(f"    {row}", file=sys.stderr)
        return 1
    print(f"    {table} matches the reference: {len(remote):,} rows, {data['key_count']:,} business keys")
    print("\nnext:\n    docker compose up -d\n    python -m puente sync --source databricks\n    python -m puente reconcile --source databricks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
