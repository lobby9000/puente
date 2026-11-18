from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
import pytest

from puente.retry import RetryPolicy, is_retryable, parse_retry_after


def test_backoff_is_full_jitter_under_the_cap():
    policy = RetryPolicy(base_delay_s=0.5, max_delay_s=4.0, seed=3)
    for retry_number in range(12):
        ceiling = min(4.0, 0.5 * 2**retry_number)
        assert all(0 <= policy.backoff(retry_number) <= ceiling for _ in range(50))


def test_retry_after_wins_over_backoff():
    policy = RetryPolicy(base_delay_s=0.25, seed=3)
    assert all(2.0 <= policy.delay(0, retry_after=2.0) <= 2.25 for _ in range(50))


def test_retry_after_is_capped():
    policy = RetryPolicy(base_delay_s=0.25, max_retry_after_s=10, seed=3)
    assert policy.delay(0, retry_after=3600) <= 10.25


def test_retry_after_accepts_seconds_and_http_dates():
    now = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(format_datetime(now + timedelta(seconds=5), usegmt=True), now=now) == pytest.approx(5.0)
    assert parse_retry_after(format_datetime(now - timedelta(seconds=5), usegmt=True), now=now) == 0.0
    assert parse_retry_after("soon") is None
    assert parse_retry_after(None) is None


@pytest.mark.parametrize(
    ("status", "body", "retry"),
    [
        (429, {}, True),
        (500, {}, True),
        (503, {}, True),
        (504, {}, True),
        (409, {"error": "request_in_progress"}, True),
        (409, {"error": "conflict"}, False),
        (400, {}, False),
        (401, {}, False),
        (422, {"error": "validation_error"}, False),
    ],
)
def test_only_transient_answers_are_retried(status, body, retry):
    assert is_retryable(httpx.Response(status, json=body)) is retry
