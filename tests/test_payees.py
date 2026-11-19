from puente.payees import from_destination, from_zoho
from puente.sources import SourceRow
from puente.transform import prepare


def payee(i: int, code: str | None = None, status: str = "active") -> dict:
    return {"payee_id": f"PY-{i:06d}", "vendor_code": code if code is not None else f"V-{i:05d}", "currency": "MXN", "status": status}


def contact(code: str, payee_id: str | None, hold: bool = False) -> dict:
    return {"Last_Name": f"Rep {code}", "Vendor_Code": code, "Payee_ID": payee_id, "Payout_Hold": hold}


def test_a_payout_goes_out_only_when_zoho_and_the_destination_agree():
    destination = [payee(1), payee(2), payee(3), payee(4, status="closed"), payee(5), payee(6, code=""), payee(7)]
    contacts = [
        contact("V-00001", "PY-000001"),
        contact("V-00002", "PY-000002", hold=True),
        contact("V-00003", None),
        contact("V-00004", "PY-000004"),
        contact("V-00008", "PY-000005"),  # copied from V-00005, which the destination knows
        contact("V-00005", "PY-000005"),
        contact("V-00009", "PY-000006"),  # two contacts claim an account the destination cannot attribute
        contact("V-00010", "PY-000006"),
        contact("V-00011", "PY-0000011"),
        contact("V-00007", "PY-000007"),
        contact("V-00007", "PY-000007"),  # the same Vendor_Code on two contacts
        {"Last_Name": "Office manager", "Vendor_Code": None, "Payee_ID": None, "Payout_Hold": None},
    ]
    directory = from_zoho(contacts, destination)
    assert set(directory.payees) == {"V-00001", "V-00005"}
    reasons = {code: reason for code, (reason, _) in directory.blocked.items()}
    assert reasons == {
        "V-00002": "payout_on_hold",
        "V-00003": "unmapped_payee",
        "V-00004": "unmapped_payee",
        "V-00008": "payee_mismatch",
        "V-00009": "payee_mismatch",
        "V-00010": "payee_mismatch",
        "V-00011": "unmapped_payee",
        "V-00007": "payee_mismatch",
    }
    assert "opened for V-00005" in directory.blocked["V-00008"][1]
    assert directory.stats["contacts_without_vendor_code"] == 1
    assert directory.lookup("V-00042") == (None, "unmapped_payee", "sales rep V-00042 is not a contact in Zoho")


def test_prepare_parks_each_rep_with_the_reason_from_the_directory():
    base = {"store_id": "MX-T001", "country": "MX", "currency": "MXN", "period": "2026-10", "financed_sales": "10", "commission_amount": "1080.00"}
    rows = [SourceRow(n, {**base, "vendor_code": code}) for n, code in enumerate(["V-00001", "V-00002", "V-00042"], start=2)]
    directory = from_zoho([contact("V-00001", "PY-000001"), contact("V-00002", "PY-000002", hold=True)], [payee(1), payee(2)])
    prepared = prepare(rows, directory)
    assert [line["payee_id"] for _, line in prepared.lines] == ["PY-000001"]
    assert [(r.row.row_number, r.reason) for r in prepared.rejections] == [(3, "payout_on_hold"), (4, "unmapped_payee")]


def test_the_destination_directory_keeps_the_stage_two_behaviour():
    directory = from_destination([payee(1), payee(2, status="closed")])
    assert set(directory.payees) == {"V-00001"}
    assert directory.lookup("V-00002")[1:] == ("unmapped_payee", "sales rep V-00002 has no active payee in the destination")
