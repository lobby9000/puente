"""The destination's payout line, as its API docs describe it.

Kept apart from the mock's own models on purpose: an integration knows the
vendor's documentation, never the vendor's code. Validating here means bad rows
are caught before they cost a request, and the server's 422 stays a backstop.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PayoutLine(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    external_id: str = Field(min_length=1, max_length=64)
    payee_id: str = Field(pattern=r"^PY-\d{6}$")
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    currency: Literal["MXN", "BRL", "COP", "PEN"]
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    memo: str | None = Field(default=None, max_length=140)

    def to_api(self) -> dict:
        line = {
            "external_id": self.external_id,
            "payee_id": self.payee_id,
            "period": self.period,
            "currency": self.currency,
            "amount": f"{self.amount:.2f}",
        }
        if self.memo:
            line["memo"] = self.memo
        return line
