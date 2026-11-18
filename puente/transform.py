"""From warehouse rows to destination payout lines, with every rejection explained."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

from pydantic import ValidationError

from .contract import PayoutLine
from .sources import SourceRow


@dataclass
class Rejection:
    row: SourceRow
    reason: str
    detail: str


@dataclass
class Prepared:
    lines: list[tuple[SourceRow, dict]] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    dropped_rows: list[int] = field(default_factory=list)  # exact copies of a row already kept


def describe_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(part) for part in err['loc']) or 'row'}: {err['msg']}" for err in exc.errors())


def memo_for(row: SourceRow) -> str | None:
    store, sales = row.data.get("store_id"), row.data.get("financed_sales")
    return f"{sales} financed sales at {store}" if store and sales else None


def prepare(rows: Iterable[SourceRow], payee_by_vendor: dict[str, dict]) -> Prepared:
    """Dedupe, map sales reps to payees and validate against the destination's contract.

    Nothing disappears quietly: an exact copy of a row is dropped and its line number
    logged, and every other problem becomes a rejection with a reason that the
    review queue keeps.
    """
    result = Prepared()
    by_key: dict[str, list[SourceRow]] = defaultdict(list)
    for row in rows:
        by_key[row.external_id].append(row)

    for key, members in by_key.items():
        unique: list[SourceRow] = []
        seen: set[tuple] = set()
        for row in members:
            fingerprint = tuple(sorted(row.data.items()))
            if fingerprint in seen:
                result.dropped_rows.append(row.row_number)
                continue
            seen.add(fingerprint)
            unique.append(row)
        if len(unique) > 1:
            # Two different amounts for the same rep and month: only a person can say which is right.
            for row in unique:
                result.rejections.append(Rejection(row, "conflicting_duplicate", f"{len(unique)} different rows share {key}"))
            continue

        row = unique[0]
        vendor = row.data.get("vendor_code", "")
        payee = payee_by_vendor.get(vendor)
        if payee is None:
            result.rejections.append(Rejection(row, "unmapped_payee", f"sales rep {vendor or '(empty)'} has no active payee in the destination"))
            continue
        try:
            line = PayoutLine(
                external_id=key,
                payee_id=payee["payee_id"],
                period=row.data.get("period", ""),
                currency=row.data.get("currency", ""),
                amount=row.data.get("commission_amount", ""),
                memo=memo_for(row),
            )
        except ValidationError as exc:
            result.rejections.append(Rejection(row, "invalid_contract", describe_errors(exc)))
            continue
        if line.currency != payee["currency"]:
            result.rejections.append(Rejection(row, "invalid_contract", f"currency {line.currency}, but the payee is paid in {payee['currency']}"))
            continue
        result.lines.append((row, line.to_api()))
    return result
