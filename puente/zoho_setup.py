"""The custom fields Puente needs in Zoho CRM, and the code that creates the missing ones.

    Accounts  Store_Code   text, unique  the store's code in the warehouse
    Contacts  Vendor_Code  text, unique  the sales rep's code in the warehouse
    Contacts  Payee_ID     text          the rep's account in the payouts tool
    Contacts  Payout_Hold  checkbox      set by Sales Ops to stop the rep's payouts

Upsert can only match records on a unique field, hence the two unique codes.
Payee_ID is left without that constraint on purpose: Puente's own check for two
reps sharing one payout account then has something to catch in the demo. In
production, making it unique as well stops that mistake at data entry.
"""

from __future__ import annotations

from dataclasses import dataclass

from .zoho import ZohoClient


@dataclass(frozen=True)
class FieldSpec:
    module: str
    api_name: str
    label: str
    data_type: str
    length: int | None = None
    unique: bool = False

    def payload(self) -> dict:
        body: dict = {"field_label": self.label, "data_type": self.data_type}
        if self.length:
            body["length"] = self.length
        if self.unique:
            body["unique"] = {"case_sensitive": False}
        return body


FIELDS = (
    FieldSpec("Accounts", "Store_Code", "Store Code", "text", 20, unique=True),
    FieldSpec("Contacts", "Vendor_Code", "Vendor Code", "text", 20, unique=True),
    FieldSpec("Contacts", "Payee_ID", "Payee ID", "text", 20),
    FieldSpec("Contacts", "Payout_Hold", "Payout Hold", "boolean"),
)


@dataclass(frozen=True)
class FieldCheck:
    spec: FieldSpec
    status: str  # ready, created, missing, renamed or failed
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status in ("ready", "created")


def check_fields(zoho: ZohoClient, created: set[str] | None = None, errors: dict[str, str] | None = None) -> list[FieldCheck]:
    """Compare what Puente needs with what the org has. Costs one credit per module."""
    created, errors = created or set(), errors or {}
    checks = []
    for module in sorted({spec.module for spec in FIELDS}):
        existing = zoho.get_fields(module)
        by_label = {str(meta.get("field_label", "")).strip().lower(): api_name for api_name, meta in existing.items()}
        for spec in (spec for spec in FIELDS if spec.module == module):
            meta = existing.get(spec.api_name)
            if meta is not None:
                detail = ""
                if meta.get("data_type") and meta["data_type"] != spec.data_type:
                    detail = f"Zoho reports it as {meta['data_type']}, Puente expects {spec.data_type}"
                checks.append(FieldCheck(spec, "created" if spec.api_name in created else "ready", detail))
            elif by_label.get(spec.label.lower()):
                other = by_label[spec.label.lower()]
                checks.append(FieldCheck(spec, "renamed", f"a field labelled {spec.label!r} exists as {other}; give it the API name {spec.api_name}"))
            elif spec.api_name in errors:
                checks.append(FieldCheck(spec, "failed", errors[spec.api_name]))
            else:
                checks.append(FieldCheck(spec, "missing"))
    return checks


def ensure_fields(zoho: ZohoClient) -> list[FieldCheck]:
    """Create the fields that are missing, then read the metadata back to confirm the API names."""
    checks = check_fields(zoho)
    missing = [check.spec for check in checks if check.status == "missing"]
    if not missing:
        return checks
    created: set[str] = set()
    errors: dict[str, str] = {}
    for module in sorted({spec.module for spec in missing}):
        specs = [spec for spec in missing if spec.module == module]
        for spec, result in zip(specs, zoho.create_fields(module, [spec.payload() for spec in specs])):
            if result.get("status") == "success":
                created.add(spec.api_name)
            else:
                errors[spec.api_name] = f"{result.get('code')}: {result.get('message')}"
    return check_fields(zoho, created, errors)
