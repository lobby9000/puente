"""Where rows come from. Stage 1 reads a CSV export of the gold table."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SourceRow:
    row_number: int
    data: dict[str, str]

    @property
    def external_id(self) -> str:
        """Business key: one commission per sales rep per month."""
        return f"{self.data.get('period', '')}:{self.data.get('vendor_code', '')}"


class CsvSource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def describe(self) -> str:
        return f"csv:{self.path}"

    def rows(self) -> Iterator[SourceRow]:
        with self.path.open(newline="", encoding="utf-8") as handle:
            for number, record in enumerate(csv.DictReader(handle), start=2):  # line 1 is the header
                yield SourceRow(number, {key: (value or "").strip() for key, value in record.items() if key})
