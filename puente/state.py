"""Local state: the run ledger and the review queue, in SQLite (standard library only)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .sources import SourceRow

# A machine can retry these on its own once the destination is healthy again.
REPLAYABLE = ("retries_exhausted", "circuit_open", "run_aborted")
# These need a person to fix the data or the setup first.
NEEDS_A_PERSON = (
    "invalid_contract",
    "unmapped_payee",
    "conflicting_duplicate",
    "rejected_by_destination",
    "payout_on_hold",
    "payee_mismatch",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id       TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    source       TEXT,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS review_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT NOT NULL,
    external_id  TEXT NOT NULL,
    source_row   INTEGER,
    reason       TEXT NOT NULL,
    detail       TEXT,
    payload_json TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'open',
    resolution   TEXT,
    attempts     INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS review_items_open ON review_items (status, reason);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StateStore:
    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # -- runs -------------------------------------------------------------------

    def start_run(self, run_id: str, kind: str, source: str) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO runs (run_id, kind, source, started_at, status) VALUES (?, ?, ?, ?, 'running')",
                (run_id, kind, source, now_iso()),
            )

    def finish_run(self, run_id: str, status: str, summary: dict) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at = ?, status = ?, summary_json = ? WHERE run_id = ?",
                (now_iso(), status, json.dumps(summary, default=str), run_id),
            )

    def recent_runs(self, limit: int = 10) -> list[dict]:
        return [dict(row) for row in self.db.execute("SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,))]

    # -- review queue -----------------------------------------------------------

    def add_reviews(self, run_id: str, items: list[tuple[SourceRow, str, str, dict | None]]) -> None:
        """Park rows with a reason. Each item is (row, reason, detail, line or None)."""
        if not items:
            return
        stamp = now_iso()
        with self.db:
            self.db.executemany(
                "INSERT INTO review_items (run_id, external_id, source_row, reason, detail, payload_json, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        run_id,
                        row.external_id,
                        row.row_number,
                        reason,
                        detail[:1000],
                        json.dumps({"row_number": row.row_number, "row": row.data, "line": line}),
                        stamp,
                        stamp,
                    )
                    for row, reason, detail, line in items
                ],
            )

    def open_items(self, reasons: tuple[str, ...] | None = None) -> list[dict]:
        sql, args = "SELECT * FROM review_items WHERE status = 'open'", []
        if reasons:
            sql += f" AND reason IN ({','.join('?' * len(reasons))})"
            args.extend(reasons)
        rows = self.db.execute(sql + " ORDER BY id", args)
        return [{**dict(row), "payload": json.loads(row["payload_json"])} for row in rows]

    def resolve(self, item_id: int, resolution: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE review_items SET status = 'resolved', resolution = ?, updated_at = ? WHERE id = ?",
                (resolution, now_iso(), item_id),
            )

    def retry_later(self, item_id: int, detail: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE review_items SET attempts = attempts + 1, detail = ?, updated_at = ? WHERE id = ?",
                (detail[:1000], now_iso(), item_id),
            )

    def reclassify(self, item_id: int, reason: str, detail: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE review_items SET reason = ?, detail = ?, attempts = attempts + 1, updated_at = ? WHERE id = ?",
                (reason, detail[:1000], now_iso(), item_id),
            )

    def counts(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        for row in self.db.execute("SELECT status, reason, COUNT(*) AS n FROM review_items GROUP BY status, reason"):
            result.setdefault(row["status"], {})[row["reason"]] = row["n"]
        return result
