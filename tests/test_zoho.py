import pytest

from puente.errors import AuthError
from puente.zoho import CreditBudgetExceeded, ZohoError, upsert_credits
from puente.zoho_setup import check_fields, ensure_fields


def reps(count: int, start: int = 1) -> list[dict]:
    return [{"Last_Name": f"Rep V-{i:05d}", "Vendor_Code": f"V-{i:05d}", "Payee_ID": f"PY-{i:06d}", "Payout_Hold": False} for i in range(start, start + count)]


def ready(zoho):
    assert all(check.usable for check in ensure_fields(zoho))
    return zoho


def test_refresh_token_buys_one_access_token_and_names_the_api_domain(zoho_server, zoho_connector):
    api = zoho_server()
    zoho = zoho_connector(api)
    assert zoho.get_org()["company_name"]
    zoho.get_fields("Contacts")
    assert zoho.api_domain == "http://testserver"
    assert zoho.metrics["token_fetches"] == 1
    assert "fake-refresh-token" not in repr(zoho)


def test_a_bad_refresh_token_is_an_auth_error_that_says_what_to_do(zoho_server, zoho_connector):
    zoho = zoho_connector(zoho_server(), refresh_token="1000.wrong")
    with pytest.raises(AuthError, match="invalid_code.*zoho auth"):
        zoho.get_org()


def test_a_grant_code_works_once(zoho_server, zoho_connector):
    api = zoho_server()
    first = zoho_connector(api, refresh_token="")
    body = first.exchange_grant_code("fake-grant-code")
    assert body["refresh_token"].startswith("1000.")
    assert first.get_org()  # the new refresh token works
    with pytest.raises(AuthError, match="invalid_code"):
        zoho_connector(api, refresh_token="").exchange_grant_code("fake-grant-code")


def test_a_missing_scope_stops_at_once_instead_of_refreshing(zoho_server, zoho_connector):
    api = zoho_server(scopes=("ZohoCRM.modules.contacts.ALL",))
    zoho = zoho_connector(api)
    with pytest.raises(AuthError, match="lacks a scope"):
        zoho.get_fields("Contacts")
    assert zoho.metrics["token_fetches"] == 1


def test_setup_creates_the_missing_fields_once(zoho_server, zoho_connector):
    api = zoho_server()
    zoho = zoho_connector(api)
    assert {check.status for check in check_fields(zoho)} == {"missing"}
    assert {check.status for check in ensure_fields(zoho)} == {"created"}
    assert {check.status for check in ensure_fields(zoho)} == {"ready"}
    assert api.app.state.zoho.stats["fields_created"] == 4


def test_upsert_inserts_then_updates_and_answers_every_record(zoho_server, zoho_connector):
    api = zoho_server()
    zoho = ready(zoho_connector(api))
    first = zoho.upsert("Contacts", reps(150), key_field="Vendor_Code")
    assert [result.action for result in first] == ["insert"] * 150
    assert zoho.credits.used >= upsert_credits(100) + upsert_credits(50)

    changed = reps(2) + [
        {"Vendor_Code": "V-99999", "Payee_ID": "PY-999999"},  # new, but without the mandatory Last_Name
        {"Last_Name": "Rep V-99998", "Vendor_Code": "V-99998", "Payout_Hold": "yes"},
    ]
    changed[0]["Payout_Hold"] = True
    second = zoho.upsert("Contacts", changed, key_field="Vendor_Code")  # HTTP 207: some made it
    assert [(result.ok, result.action, result.code, result.field) for result in second] == [
        (True, "update", "SUCCESS", None),
        (True, "update", "SUCCESS", None),
        (False, None, "MANDATORY_NOT_FOUND", "Last_Name"),
        (False, None, "INVALID_DATA", "Payout_Hold"),
    ]
    contacts = list(zoho.iter_records("Contacts", ["Vendor_Code", "Payout_Hold"]))
    assert len(contacts) == 150
    assert {c["Vendor_Code"] for c in contacts if c["Payout_Hold"]} == {"V-00001"}


def test_a_refused_call_raises_instead_of_looking_like_record_errors(zoho_server, zoho_connector):
    zoho = ready(zoho_connector(zoho_server()))
    with pytest.raises(ZohoError, match="duplicate_check_fields"):
        zoho.upsert("Contacts", reps(3), key_field="Last_Name")  # not a unique field


def test_retrying_a_lost_upsert_answer_does_not_duplicate_records(zoho_server, zoho_connector):
    api = zoho_server(chaos={"lost_write_rate": 0.4, "seed": 3})
    zoho = ready(zoho_connector(api, max_attempts=10))
    results = zoho.upsert("Contacts", reps(500), key_field="Vendor_Code")
    state = api.app.state.zoho
    assert all(result.ok for result in results)
    assert state.stats["injected_lost_writes"] > 0
    assert len(state.modules["Contacts"].records) == 500  # the retries updated, never inserted twice
    assert {result.action for result in results} == {"insert", "update"}


def test_throttling_errors_and_revoked_tokens_are_absorbed(zoho_server, zoho_connector):
    api = zoho_server(chaos={"throttle_rate": 0.2, "error_rate": 0.1, "token_revoke_rate": 0.1, "seed": 8})
    zoho = ready(zoho_connector(api, max_attempts=12))
    zoho.upsert("Contacts", reps(450), key_field="Vendor_Code")
    assert len(list(zoho.iter_records("Contacts", ["Vendor_Code"]))) == 450
    stats = api.app.state.zoho.stats
    assert stats["injected_throttles"] and stats["injected_errors"] and stats["injected_token_revocations"]
    assert zoho.metrics["retries"] and zoho.metrics["token_refused"]


def test_pagination_with_page_numbers_then_page_tokens(zoho_server, zoho_connector):
    api = zoho_server(page_token_from_page=10)  # like older versions: no token before page 10
    zoho = ready(zoho_connector(api))
    zoho.upsert("Contacts", reps(250), key_field="Vendor_Code")
    codes = [record["Vendor_Code"] for record in zoho.iter_records("Contacts", ["Vendor_Code"], per_page=20)]
    assert len(codes) == len(set(codes)) == 250
    assert zoho.metrics["pages_read"] == 13


def test_an_empty_module_answers_204(zoho_server, zoho_connector):
    zoho = ready(zoho_connector(zoho_server()))
    assert list(zoho.iter_records("Accounts", ["Store_Code"])) == []


def test_the_run_budget_stops_a_call_before_it_is_made(zoho_server, zoho_connector):
    api = zoho_server()
    zoho = ready(zoho_connector(api, budget=60))  # setup took 2 reads, 40 for fields, 2 reads
    with pytest.raises(CreditBudgetExceeded, match="PUENTE_ZOHO_CREDIT_BUDGET"):
        zoho.upsert("Contacts", reps(200), key_field="Vendor_Code")
    assert len(api.app.state.zoho.modules["Contacts"].records) == 0


def test_the_reserve_leaves_credits_for_the_rest_of_the_org(zoho_server, zoho_connector):
    api = zoho_server(daily_credits=200)
    zoho = ready(zoho_connector(api, budget=10_000, reserve=60))
    with pytest.raises(CreditBudgetExceeded, match="PUENTE_ZOHO_CREDIT_RESERVE"):
        for start in range(1, 3000, 100):
            zoho.upsert("Contacts", reps(100, start), key_field="Vendor_Code")
    state = api.app.state.zoho
    assert zoho.credits.remaining is not None  # Zoho started reporting the pool after half of it went
    assert 200 - state.credits_used >= 60
