"""Account for every business key across the source, the destination and the review queue."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .sources import SourceRow


def as_amount(value) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, TypeError):
        return None


@dataclass
class Reconciliation:
    source_rows: int
    source_keys: int
    delivered_once: int = 0
    in_review: int = 0
    lost: list[str] = field(default_factory=list)
    paid_twice: list[str] = field(default_factory=list)
    wrong_amount: list[str] = field(default_factory=list)
    delivered_and_in_review: list[str] = field(default_factory=list)
    not_in_source: list[str] = field(default_factory=list)
    open_review_by_reason: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        """No key lost, none paid twice, none with a wrong amount, none in two places."""
        return not (self.lost or self.paid_twice or self.wrong_amount or self.delivered_and_in_review)


def reconcile(rows: Iterable[SourceRow], payouts: Iterable[dict], open_review: Iterable[dict]) -> Reconciliation:
    by_key: dict[str, list[SourceRow]] = defaultdict(list)
    row_count = 0
    for row in rows:
        by_key[row.external_id].append(row)
        row_count += 1
    paid: dict[str, list[dict]] = defaultdict(list)
    for payout in payouts:
        paid[payout["external_id"]].append(payout)
    review = list(open_review)
    review_keys = {item["external_id"] for item in review}

    result = Reconciliation(
        source_rows=row_count,
        source_keys=len(by_key),
        open_review_by_reason=dict(Counter(item["reason"] for item in review)),
    )
    for key, members in by_key.items():
        landed = paid.get(key, [])
        if not landed:
            if key in review_keys:
                result.in_review += 1
            else:
                result.lost.append(key)
            continue
        if len(landed) > 1:
            result.paid_twice.append(key)
        else:
            result.delivered_once += 1
        if key in review_keys:
            result.delivered_and_in_review.append(key)
        expected = {as_amount(member.data.get("commission_amount")) for member in members}
        if any(as_amount(payout["amount"]) not in expected for payout in landed):
            result.wrong_amount.append(key)
    result.not_in_source = sorted(key for key in paid if key not in by_key)
    return result
