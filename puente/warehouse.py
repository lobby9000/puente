"""Databricks as the source: read the gold table through a SQL warehouse.

Needs the optional extra (pip install -e ".[databricks]") and three settings:
DATABRICKS_HOST, DATABRICKS_HTTP_PATH and DATABRICKS_TOKEN. Keep the token in .env.
Anything with a DB-API cursor works as the connection, which is how the tests run
the same code against DuckDB.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx

from .errors import PuenteError
from .sources import SourceRow

TABLE_NAME = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$")


def normalize_host(host: str) -> str:
    return host.strip().removeprefix("https://").removeprefix("http://").rstrip("/")


def connect(host: str, http_path: str, token: str):
    host = normalize_host(host)
    if "." not in host or not http_path.startswith("/sql/"):
        # Catch typos up front: the driver would keep retrying a wrong address for minutes.
        raise PuenteError("DATABRICKS_HOST should look like dbc-1234.cloud.databricks.com and DATABRICKS_HTTP_PATH like /sql/1.0/warehouses/abcd1234")
    try:
        from databricks import sql
    except ImportError as exc:
        raise PuenteError('install the Databricks extra first: pip install -e ".[databricks]"') from exc
    return sql.connect(server_hostname=host, http_path=http_path, access_token=token)


class DatabricksSource:
    def __init__(self, table: str, connect: Callable[[], object]) -> None:
        if not TABLE_NAME.match(table):
            raise ValueError(f"not a table name: {table!r}")
        self.table = table
        self._connect = connect

    def describe(self) -> str:
        return f"databricks:{self.table}"

    def rows(self) -> Iterator[SourceRow]:
        connection = self._connect()
        try:
            cursor = connection.cursor()
            try:
                cursor.execute(f"SELECT * FROM {self.table} ORDER BY period, vendor_code, commission_rate")
                columns = [column[0] for column in cursor.description]
                records = cursor.fetchall()
            finally:
                cursor.close()
        finally:
            connection.close()
        for number, record in enumerate(records, start=1):
            yield SourceRow(number, {name: "" if value is None else str(value) for name, value in zip(columns, record)})


def split_statements(text: str) -> list[str]:
    """Statements of a .sql file. Comments are full lines starting with --."""
    kept = [line for line in text.splitlines() if not line.strip().startswith("--")]
    return [statement.strip() for statement in "\n".join(kept).split(";") if statement.strip()]


def render(path: str | Path, replacements: dict[str, str]) -> str:
    text = Path(path).read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace("{" + key + "}", value)
    return text


def run_sql_file(connection, path: str | Path, replacements: dict[str, str]) -> int:
    statements = split_statements(render(path, replacements))
    cursor = connection.cursor()
    try:
        for statement in statements:
            cursor.execute(statement)
    finally:
        cursor.close()
    return len(statements)


def upload_to_volume(host: str, token: str, local_path: str | Path, volume_path: str, *, http: httpx.Client | None = None) -> None:
    """PUT a file into a Unity Catalog volume with the Files API (up to 5 GiB per file)."""
    url = f"https://{normalize_host(host)}/api/2.0/fs/files{volume_path}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream"}
    content = Path(local_path).read_bytes()
    if http is None:
        with httpx.Client(timeout=300) as client:
            response = client.put(url, params={"overwrite": "true"}, content=content, headers=headers)
    else:
        response = http.put(url, params={"overwrite": "true"}, content=content, headers=headers)
    if response.status_code >= 300:
        raise PuenteError(f"upload of {volume_path} failed: HTTP {response.status_code} {response.text[:300]}")
