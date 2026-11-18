from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_saas.app import create_app
from mock_saas.config import ChaosConfig, ServerConfig
from puente.breaker import CircuitBreaker
from puente.client import SaaSClient
from puente.retry import RetryPolicy


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
