"""Structured logs: one JSON object per line, easy to search and to ship anywhere."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_STANDARD_FIELDS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "event": record.getMessage(),
        }
        entry.update({key: value for key, value in vars(record).items() if key not in _STANDARD_FIELDS and not key.startswith("_")})
        if record.exc_info:
            entry["error"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def setup_logging(log_file: str | None = None, *, console_level: int = logging.WARNING) -> None:
    logger = logging.getLogger("puente")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.handlers.clear()
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(console_level)
    console.setFormatter(JsonFormatter())
    logger.addHandler(console)
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        to_file = logging.FileHandler(log_file, encoding="utf-8")
        to_file.setFormatter(JsonFormatter())
        logger.addHandler(to_file)
