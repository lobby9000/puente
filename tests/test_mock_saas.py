def auth(api) -> dict:
    response = api.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": "puente-dev", "client_secret": "dev-secret"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def line(n: int, **changes) -> dict:
    """A valid line for payee n. Payees 1 to 4 are paid in MXN."""
    return {"external_id": f"2026-10:V-{n:05d}", "payee_id": f"PY-{n:06d}", "period": "2026-10", "currency": "MXN", "amount": "1200.50", **changes}


def post(api, headers: dict, key: str, lines: list[dict]):
    return api.post("/v1/payout-batches", json={"lines": lines}, headers={**headers, "Idempotency-Key": key})


def test_calls_without_a_token_get_401(server):
    assert server().get("/v1/payees").status_code == 401


def test_rate_limit_answers_429_with_retry_after(server):
    api = server(rate_limit=True)
    headers = auth(api)
    limited = [answer for answer in (api.get("/v1/payees", headers=headers) for _ in range(30)) if answer.status_code == 429]
    assert limited and int(limited[0].headers["Retry-After"]) >= 1


def test_same_key_and_payload_is_written_once(server):
    api = server()
    headers = auth(api)
    first = post(api, headers, "k1", [line(1)])
    second = post(api, headers, "k1", [line(1)])
    assert first.status_code == second.status_code == 201
    assert second.headers["Idempotent-Replayed"] == "true"
    assert first.json()["batch_id"] == second.json()["batch_id"]
    assert len(api.app.state.mock.payouts) == 1


def test_same_key_with_another_payload_is_refused(server):
    api = server()
    headers = auth(api)
    post(api, headers, "k1", [line(1)])
    answer = post(api, headers, "k1", [line(2)])
    assert answer.status_code == 422 and answer.json()["error"] == "idempotency_key_reused"


def test_a_lost_answer_still_writes_and_the_retry_replays_it(server):
    api = server(ghost_write_rate=1.0, seed=1)
    headers = auth(api)
    assert post(api, headers, "k1", [line(1)]).status_code == 504
    assert len(api.app.state.mock.payouts) == 1
    retry = post(api, headers, "k1", [line(1)])
    assert retry.status_code == 201 and retry.headers["Idempotent-Replayed"] == "true"
    assert len(api.app.state.mock.payouts) == 1


def test_schema_errors_name_the_line_and_nothing_is_written(server):
    api = server()
    answer = post(api, auth(api), "k1", [line(1), line(2, currency="USD")])
    assert answer.status_code == 422
    assert [item["index"] for item in answer.json()["invalid_lines"]] == [1]
    assert api.app.state.mock.payouts == []


def test_business_rules_name_the_line(server):
    api = server()
    answer = post(api, auth(api), "k1", [line(1), line(2, amount="900000.00"), line(3, payee_id="PY-999999")])
    flagged = {item["index"]: item["errors"] for item in answer.json()["invalid_lines"]}
    assert answer.status_code == 422 and sorted(flagged) == [1, 2]
    assert "cap" in flagged[1][0] and "unknown payee" in flagged[2][0]
    assert api.app.state.mock.payouts == []


def test_an_outage_refuses_every_call(server):
    api = server(outage_at_request=1, outage_seconds=30)
    headers = auth(api)
    assert post(api, headers, "k1", [line(1)]).status_code == 503
    assert api.get("/v1/payees", headers=headers).status_code == 503


def test_payouts_can_be_looked_up_by_external_id(server):
    api = server()
    headers = auth(api)
    post(api, headers, "k1", [line(1), line(2), line(3)])
    found = api.get("/v1/payouts", params={"external_id": ["2026-10:V-00001", "2026-10:V-00003"]}, headers=headers).json()["data"]
    assert sorted(payout["external_id"] for payout in found) == ["2026-10:V-00001", "2026-10:V-00003"]
