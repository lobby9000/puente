"""When to retry, and how long to wait.

Retryable: network errors and timeouts, 408, 425, 429, 5xx, and the 409 a server
sends while an earlier request with the same Idempotency-Key is still running.
Anything else is an answer, not a hiccup, and goes back to the caller.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
RETRYABLE_EXCEPTIONS = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)


@dataclass
class RetryPolicy:
    max_attempts: int = 6
    base_delay_s: float = 0.25
    max_delay_s: float = 8.0
    max_elapsed_s: float = 30.0
    max_retry_after_s: float = 60.0
    seed: int | None = None
    _rng: random.Random = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    def backoff(self, retry_number: int) -> float:
        """Full jitter: a uniform draw between 0 and base * 2^n, capped at max_delay_s."""
        ceiling = min(self.max_delay_s, self.base_delay_s * (2**retry_number))
        return self._rng.uniform(0, ceiling)

    def delay(self, retry_number: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            # The server said when to come back. Wait that long, plus a little jitter
            # so that many clients do not all return on the same tick.
            return min(retry_after, self.max_retry_after_s) + self._rng.uniform(0, self.base_delay_s)
        return self.backoff(retry_number)


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Retry-After holds either a number of seconds or an HTTP date (RFC 9110, 10.2.3)."""
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


def error_code(response: httpx.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("error") if isinstance(body, dict) else None


def is_retryable(response: httpx.Response) -> bool:
    if response.status_code in RETRYABLE_STATUS:
        return True
    return response.status_code == 409 and error_code(response) == "request_in_progress"
