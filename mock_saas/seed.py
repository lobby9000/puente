"""The sales-rep universe, shared by the mock SaaS and the demo data generator.

Sales rep i is vendor V-0000i in the warehouse and payee PY-00000i in the
destination. Countries repeat in a fixed cycle, so both sides agree on who is
paid in which currency without talking to each other.
"""

from __future__ import annotations

COUNTRY_CYCLE = ("MX", "MX", "MX", "MX", "MX", "CO", "CO", "PE", "PE", "BR")
CURRENCY_BY_COUNTRY = {"MX": "MXN", "CO": "COP", "PE": "PEN", "BR": "BRL"}


def vendor_code(i: int) -> str:
    return f"V-{i:05d}"


def payee_id(i: int) -> str:
    return f"PY-{i:06d}"


def country_for(i: int) -> str:
    return COUNTRY_CYCLE[i % len(COUNTRY_CYCLE)]


def seed_payees(count: int) -> list[dict]:
    payees = []
    for i in range(1, count + 1):
        country = country_for(i)
        payees.append(
            {
                "payee_id": payee_id(i),
                "vendor_code": vendor_code(i),
                "name": f"Sales rep {i:05d}",
                "country": country,
                "currency": CURRENCY_BY_COUNTRY[country],
                "status": "active",
            }
        )
    return payees
