import httpx
import pytest

from puente.client import SaaSClient
from puente.errors import AuthError, RetryExhausted
from puente.retry import RetryPolicy


class ScriptedSaaS:
    """A transport that always grants tokens and answers API calls from a script."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[httpx.Request] = []
        self.tokens_issued = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/oauth/token":
            self.tokens_issued += 1
            return httpx.Response(200, json={"access_token": f"t{self.tokens_issued}", "token_type": "Bearer", "expires_in": 300})
        self.calls.append(request)
        step = self.script.pop(0) if self.script else httpx.Response(201, json={"batch_id": "b1"})
        if isinstance(step, Exception):
            raise step
        return step


def client_for(fake: ScriptedSaaS, sleeps: list[float] | None = None, **retry) -> SaaSClient:
    http = httpx.Client(transport=httpx.MockTransport(fake), base_url="http://test")
    sleep = sleeps.append if sleeps is not None else (lambda _seconds: None)
    return SaaSClient("http://test", "id", "secret", http=http, retry=RetryPolicy(seed=1, **retry), sleep=sleep)


def test_retries_transient_errors_then_succeeds():
    fake = ScriptedSaaS(httpx.Response(503), httpx.Response(502), httpx.Response(201, json={"batch_id": "b1"}))
    assert client_for(fake).create_payout_batch([], "key-1").status_code == 201
    assert len(fake.calls) == 3


def test_waits_as_long_as_retry_after_says():
    sleeps: list[float] = []
    fake = ScriptedSaaS(httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(201, json={}))
    client_for(fake, sleeps, base_delay_s=0.25).create_payout_batch([], "key-1")
    assert len(sleeps) == 1 and 3.0 <= sleeps[0] <= 3.25


def test_a_retry_after_a_timeout_reuses_the_idempotency_key():
    timeout = httpx.ReadTimeout("no answer", request=httpx.Request("POST", "http://test/v1/payout-batches"))
    fake = ScriptedSaaS(timeout, httpx.Response(201, json={}))
    client_for(fake).create_payout_batch([], "key-1")
    assert [call.headers["Idempotency-Key"] for call in fake.calls] == ["key-1", "key-1"]


def test_refreshes_the_token_after_a_401():
    fake = ScriptedSaaS(httpx.Response(401, json={"error": "invalid_token"}), httpx.Response(201, json={}))
    client_for(fake).create_payout_batch([], "key-1")
    assert [call.headers["Authorization"] for call in fake.calls] == ["Bearer t1", "Bearer t2"]


def test_gives_up_when_the_attempts_run_out():
    fake = ScriptedSaaS(*[httpx.Response(503) for _ in range(10)])
    with pytest.raises(RetryExhausted):
        client_for(fake, max_attempts=4).create_payout_batch([], "key-1")
    assert len(fake.calls) == 4


def test_does_not_retry_a_validation_error():
    fake = ScriptedSaaS(httpx.Response(422, json={"error": "validation_error", "invalid_lines": []}))
    assert client_for(fake).create_payout_batch([], "key-1").status_code == 422
    assert len(fake.calls) == 1


def test_wrong_credentials_raise_auth_error(server):
    client = SaaSClient("http://testserver", "puente-dev", "wrong-secret", http=server(), sleep=lambda _seconds: None)
    with pytest.raises(AuthError):
        client.list_payees()


def test_pagination_reads_every_page(server, connector):
    payees = connector(server(payee_count=250)).list_payees()
    assert len(payees) == 250
    assert len({payee["payee_id"] for payee in payees}) == 250
