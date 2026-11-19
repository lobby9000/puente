"""Who can be paid, and into which account.

Stages 1 and 2 take the answer from the payouts tool: an active payee whose
vendor_code matches the rep. From stage 3 on, the sales network lives in Zoho
CRM, the system Sales Ops keeps up to date: each rep is a Contact with a
Vendor_Code, the payout account the team captured for them (Payee_ID) and a hold
flag (Payout_Hold). The payouts tool still knows whether that account exists, is
active, which currency it pays in and which rep it was opened for.

A payout only goes out when both systems agree. When they do not, the rep's
lines wait in the review queue with the reason, because only a person can tell
which system is wrong:

    payout_on_hold   Sales Ops checked Payout_Hold on the contact
    unmapped_payee   no contact, no Payee_ID, or an account the payouts tool lacks or has closed
    payee_mismatch   the account was opened for another rep, or two contacts claim it
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field

CONTACT_FIELDS = ("Last_Name", "Vendor_Code", "Payee_ID", "Payout_Hold")


@dataclass
class PayeeDirectory:
    source: str
    payees: dict[str, dict] = field(default_factory=dict)  # vendor_code -> payee in the payouts tool
    blocked: dict[str, tuple[str, str]] = field(default_factory=dict)  # vendor_code -> (reason, detail)
    stats: Counter[str] = field(default_factory=Counter)
    missing_detail: str = "sales rep {code} has no active payee in the destination"

    def lookup(self, code: str) -> tuple[dict | None, str, str]:
        """The payee for a rep, or None with the reason and detail for the review queue."""
        if code in self.payees:
            return self.payees[code], "", ""
        if code in self.blocked:
            reason, detail = self.blocked[code]
            return None, reason, detail
        return None, "unmapped_payee", self.missing_detail.format(code=code or "(empty)")


def from_destination(destination_payees: Iterable[dict]) -> PayeeDirectory:
    directory = PayeeDirectory(source="destination")
    for payee in destination_payees:
        if payee.get("status") == "active" and payee.get("vendor_code"):
            directory.payees[payee["vendor_code"]] = payee
    directory.stats["usable"] = len(directory.payees)
    return directory


def text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def from_zoho(contacts: Iterable[dict], destination_payees: Iterable[dict]) -> PayeeDirectory:
    """Map reps to payees from the Zoho contacts, checked against the payouts tool."""
    directory = PayeeDirectory(source="zoho", missing_detail="sales rep {code} is not a contact in Zoho")
    by_id = {payee["payee_id"]: payee for payee in destination_payees if payee.get("payee_id")}
    reps: dict[str, dict] = {}
    claims: dict[str, list[str]] = defaultdict(list)
    for contact in contacts:
        directory.stats["contacts_read"] += 1
        code = text(contact.get("Vendor_Code"))
        if not code:
            directory.stats["contacts_without_vendor_code"] += 1  # not a commissioned rep
            continue
        if code in reps:
            directory.blocked[code] = ("payee_mismatch", f"Vendor_Code {code} is on more than one Zoho contact")
            continue
        reps[code] = contact
        payee_id = text(contact.get("Payee_ID"))
        if payee_id:
            claims[payee_id].append(code)

    for code, contact in reps.items():
        if code in directory.blocked:
            continue
        payee_id = text(contact.get("Payee_ID"))
        payee = by_id.get(payee_id)
        owner = text(payee.get("vendor_code")) if payee else ""
        if contact.get("Payout_Hold") is True:
            blocked = ("payout_on_hold", f"Payout_Hold is checked on the Zoho contact of {code}")
        elif not payee_id:
            blocked = ("unmapped_payee", f"sales rep {code} has no Payee_ID in Zoho")
        elif payee is None:
            blocked = ("unmapped_payee", f"Payee_ID {payee_id} from Zoho does not exist in the destination")
        elif payee.get("status") != "active":
            blocked = ("unmapped_payee", f"payee {payee_id} is {payee.get('status')} in the destination")
        elif owner and owner != code:
            # Without this check the payouts tool would accept the line and pay the other rep.
            blocked = ("payee_mismatch", f"Zoho gives {code} the payee {payee_id}, which the destination opened for {owner}")
        elif not owner and len(claims[payee_id]) > 1:
            blocked = ("payee_mismatch", f"payee {payee_id} is claimed by {', '.join(sorted(claims[payee_id]))} in Zoho")
        else:
            directory.payees[code] = payee
            continue
        directory.blocked[code] = blocked

    directory.stats["usable"] = len(directory.payees)
    for reason, _ in directory.blocked.values():
        directory.stats[f"blocked:{reason}"] += 1
    return directory
