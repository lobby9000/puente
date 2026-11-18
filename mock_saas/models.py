"""Public contract of the mock payouts SaaS: what its API docs would publish."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Currency = Literal["MXN", "BRL", "COP", "PEN"]

# Per-line compliance cap. Only the server enforces it, the way a downstream
# business rule usually surprises an integration the first time.
PAYOUT_CAPS: dict[str, Decimal] = {
    "MXN": Decimal("50000"),
    "BRL": Decimal("15000"),
    "COP": Decimal("12000000"),
    "PEN": Decimal("10000"),
}


class PayoutLine(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=64)
    payee_id: str = Field(pattern=r"^PY-\d{6}$")
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    currency: Currency
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    memo: str | None = Field(default=None, max_length=140)


class PayoutBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lines: list[PayoutLine] = Field(min_length=1)
