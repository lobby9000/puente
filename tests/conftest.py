from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_saas.app import create_app
from mock_saas.config import ChaosConfig, ServerConfig
from mock_zoho.app import ZohoChaos, ZohoConfig
from mock_zoho.app import create_app as create_zoho_app
from puente.breaker import CircuitBreaker
from puente.client import SaaSClient
from puente.retry import RetryPolicy
from puente.zoho import CreditMeter, ZohoClient


def no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
def server():
    """An in-process mock SaaS. Rate limit off unless asked, chaos as given."""

    def build(payee_count: int = 60, rate_limit: bool = False, **chaos) -> TestClient:
        config = ServerConfig(payee_count=payee_count, rate_limit_enabled=rate_limit, chaos=ChaosConfig(**chaos))
        return TestClient(create_app(config))

    return build


@pytest.fixture
def connector():
    """A Puente client wired to an in-process server, with sleeping turned off."""

    def build(http, **retry) -> SaaSClient:
        return SaaSClient("http://testserver", "puente-dev", "dev-secret", http=http, retry=RetryPolicy(seed=1, **retry), sleep=no_sleep)

    return build


@pytest.fixture
def breaker() -> CircuitBreaker:
    return CircuitBreaker(failure_threshold=3, cool_down_s=0, max_open_s=60, sleep=no_sleep)


@pytest.fixture
def zoho_server():
    """An in-process fake Zoho CRM. Options go to ZohoConfig, chaos to ZohoChaos."""

    def build(*, chaos: dict | None = None, **options) -> TestClient:
        config = ZohoConfig(api_domain="http://testserver", chaos=ZohoChaos(**(chaos or {})), **options)
        return TestClient(create_zoho_app(config))

    return build


@pytest.fixture
def zoho_connector():
    """A Zoho client wired to an in-process fake, with sleeping turned off."""

    def build(http, *, refresh_token: str = "fake-refresh-token", budget: int = 1000, reserve: int = 0, **retry) -> ZohoClient:
        return ZohoClient(
            "fake-client-id",
            "fake-client-secret",
            refresh_token,
            accounts_url="http://testserver",
            http=http,
            retry=RetryPolicy(seed=1, **retry),
            credits=CreditMeter(budget=budget, reserve=reserve),
            sleep=no_sleep,
        )

    return build
