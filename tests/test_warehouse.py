"""The warehouse SQL runs here on DuckDB, with the same files Databricks runs.

Only bronze differs: Databricks reads the files with read_files from a volume,
these tests load them with DuckDB's read_csv. Silver and gold are the same SQL.
"""

from pathlib import Path

import duckdb
import httpx
import pytest

from demo.generate import COLUMNS
from demo.generate_raw import PLAN_FILE, SALES_FILE, generate_raw
from puente.reconcile import reconcile
from puente.state import StateStore
from puente.sync import run_sync
from puente.warehouse import DatabricksSource, normalize_host, render, run_sql_file, split_statements, upload_to_volume

SQL_DIR = Path(__file__).resolve().parent.parent / "warehouse"
REPLACEMENTS = {"target": "workspace.puente", "volume": "/Volumes/workspace/puente/raw"}


def build_warehouse(tmp_path: Path, vendors: int = 40, months: int = 3, seed: int = 4):
    raw = tmp_path / "raw"
    data = generate_raw(vendors, months, seed, raw)
    path = str(tmp_path / "warehouse.duckdb")
    con = duckdb.connect(path)
    con.execute("CREATE SCHEMA puente")
    for table, name in (("bronze_sales", SALES_FILE), ("bronze_comp_plan", PLAN_FILE)):
        con.execute(f"CREATE TABLE puente.{table} AS SELECT * FROM read_csv('{raw / name}', header = true, all_varchar = true)")
    for name in ("02_silver.sql", "03_gold.sql"):
        run_sql_file(con, SQL_DIR / name, {"target": "puente"})
    con.close()
    return path, data


def gold_source(path: str) -> DatabricksSource:
    return DatabricksSource("puente.gold_vendor_commissions", connect=lambda: duckdb.connect(path))


def test_gold_sql_matches_the_python_reference(tmp_path):
    path, data = build_warehouse(tmp_path)
    rows = sorted(tuple(row.data[column] for column in COLUMNS) for row in gold_source(path).rows())
    assert rows == sorted(tuple(row[column] for column in COLUMNS) for row in data["gold"])
    assert len(rows) == data["gold_rows"] > data["key_count"]  # the plan conflict adds rows for the same keys


def test_silver_keeps_one_row_per_sale(tmp_path):
    path, data = build_warehouse(tmp_path)
    con = duckdb.connect(path)
    bronze = con.execute("SELECT COUNT(*) FROM puente.bronze_sales").fetchone()[0]
    silver = con.execute("SELECT COUNT(*) FROM puente.silver_sales").fetchone()[0]
    con.close()
    assert bronze - silver == data["duplicate_sale_rows"] > 0


def test_stage_2_rows_end_where_the_ground_truth_says(tmp_path, server, connector, breaker):
    path, data = build_warehouse(tmp_path)
    rows = list(gold_source(path).rows())
    api = server(payee_count=40, error_rate=0.1, ghost_write_rate=0.1, seed=3)
    store = StateStore(tmp_path / "state.db")
    client = connector(api, max_attempts=8)
    run_sync(client, store, rows, source="duckdb", batch_size=10, breaker=breaker)

    payouts = list(client.iter_payouts())
    audit = reconcile(rows, payouts, store.open_items())
    assert audit.clean and not audit.not_in_source
    paid = {payout["external_id"] for payout in payouts}
    parked = {item["external_id"]: item["reason"] for item in store.open_items()}
    for key, expected in data["keys"].items():
        if expected == "paid":
            assert key in paid, key
        else:
            assert parked.get(key) == expected.split(":", 1)[1], key
    assert set(data["expected"]) == {"paid", "review:conflicting_duplicate", "review:invalid_contract", "review:rejected_by_destination", "review:unmapped_payee"}


def test_sql_files_render_into_plain_statements():
    for name, count in (("00_setup.sql", 2), ("01_bronze.sql", 2), ("02_silver.sql", 2), ("03_gold.sql", 1)):
        statements = split_statements(render(SQL_DIR / name, REPLACEMENTS))
        assert len(statements) == count, name
        assert not any("{target}" in s or "{volume}" in s or s.startswith("--") for s in statements)
    bronze = split_statements(render(SQL_DIR / "01_bronze.sql", REPLACEMENTS))
    assert "'/Volumes/workspace/puente/raw/sales_raw.csv'" in bronze[0]


def test_upload_puts_the_bytes_with_the_files_api(tmp_path):
    file = tmp_path / "x.csv"
    file.write_bytes(b"a,b\n1,2\n")
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        upload_to_volume("https://dbc-123.cloud.databricks.com/", "token-1", file, "/Volumes/workspace/puente/raw/x.csv", http=http)
    request = seen[0]
    assert request.method == "PUT"
    assert str(request.url) == "https://dbc-123.cloud.databricks.com/api/2.0/fs/files/Volumes/workspace/puente/raw/x.csv?overwrite=true"
    assert request.headers["Authorization"] == "Bearer token-1"
    assert request.content == b"a,b\n1,2\n"


def test_hosts_and_table_names_are_checked():
    assert normalize_host(" https://dbc-123.cloud.databricks.com/ ") == "dbc-123.cloud.databricks.com"
    with pytest.raises(ValueError):
        DatabricksSource("gold; DROP TABLE x", connect=lambda: None)
