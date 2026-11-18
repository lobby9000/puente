"""HTTP client for the payouts SaaS: OAuth tokens, retries, pagination and idempotency."""

from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator

import httpx

from .errors import AuthError, RetryExhausted, UnexpectedResponse
from .retry import RETRYABLE_EXCEPTIONS, RetryPolicy, is_retryable, parse_retry_after

log = logging.getLogger("puente.client")

MAX_TOKEN_REFRESHES_PER_CALL = 2
MAX_IDS_PER_QUERY = 100


class SaaSClient:
    def __init__(
        self,
        base_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout_s: float = 2.0,
        retry: RetryPolicy | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http or httpx.Client(base_url=base_url, timeout=timeout_s)
        self._owns_http = http is None
        self._client_id = client_id
        self._client_secret = client_secret
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.metrics: Counter[str] = Counter()

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> SaaSClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        response = self._send(
            "POST",
            "/oauth/token",
            data={"grant_type": "client_credentials", "client_id": self._client_id, "client_secret": self._client_secret},
            authenticated=False,
        )
        if response.status_code != 200:
            raise AuthError(f"token request refused: HTTP {response.status_code} {response.text[:200]}")
        body = response.json()
        ttl = float(body["expires_in"])
        self._token = body["access_token"]
        # Renew a little before the server's deadline, so a token never expires mid-call.
        self._token_expires_at = self._clock() + ttl - min(30.0, ttl * 0.1)
        self.metrics["token_fetches"] += 1
        return self._token

    # -- calls with retries ---------------------------------------------------

    def request(self, method: str, path: str, *, params=None, json=None, idempotency_key: str | None = None) -> httpx.Response:
        """Authenticated call with retries.

        Returns the final answer, a success or a non-retryable error such as a 422.
        Raises RetryExhausted when retryable failures outlast the retry budget and
        AuthError when the credentials themselves are refused.
        """
        return self._send(method, path, params=params, json=json, idempotency_key=idempotency_key, authenticated=True)

    def _send(self, method, path, *, params=None, json=None, data=None, idempotency_key=None, authenticated=True) -> httpx.Response:
        started = self._clock()
        attempt = 0
        refreshes = 0
        while True:
            attempt += 1
            headers = {}
            if idempotency_key:
                # The same key on every attempt. That is what makes a retry safe after a
                # timeout, when there is no way to know whether the server already wrote.
                headers["Idempotency-Key"] = idempotency_key
            if authenticated:
                headers["Authorization"] = f"Bearer {self._access_token()}"
            retry_after = None
            try:
                response = self._http.request(method, path, params=params, json=json, data=data, headers=headers)
            except RETRYABLE_EXCEPTIONS as exc:
                outcome = type(exc).__name__
            else:
                if response.status_code == 401 and authenticated:
                    if refreshes >= MAX_TOKEN_REFRESHES_PER_CALL:
                        raise AuthError(f"{method} {path}: token refused again after {refreshes} refreshes")
                    refreshes += 1
                    attempt -= 1  # a refresh is not a failed attempt
                    self._token = None
                    self.metrics["token_refused"] += 1
                    log.warning("token_refused", extra={"method": method, "path": path, "refresh": refreshes})
                    continue
                if not is_retryable(response):
                    return response
                outcome = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))

            if attempt >= self._retry.max_attempts:
                raise RetryExhausted(f"{method} {path}: {attempt} attempts, last {outcome}", attempts=attempt, last_outcome=outcome)
            wait = self._retry.delay(attempt - 1, retry_after)
            if self._clock() - started + wait > self._retry.max_elapsed_s:
                raise RetryExhausted(
                    f"{method} {path}: retry budget of {self._retry.max_elapsed_s:g} s spent after {attempt} attempts, last {outcome}",
                    attempts=attempt,
                    last_outcome=outcome,
                )
            self.metrics["retries"] += 1
            self.metrics[f"retry:{outcome}"] += 1
            log.info(
                "retrying",
                extra={"method": method, "path": path, "attempt": attempt, "outcome": outcome, "wait_s": round(wait, 3), "idempotency_key": idempotency_key},
            )
            self._sleep(wait)

    # -- API ------------------------------------------------------------------

    def iter_pages(self, path: str, *, params: dict | None = None, page_size: int = 100) -> Iterator[dict]:
        """Follow next_cursor until the last page."""
        cursor = None
        while True:
            query = dict(params or {})
            query["limit"] = page_size
            if cursor:
                query["cursor"] = cursor
            response = self.request("GET", path, params=query)
            if response.status_code != 200:
                raise UnexpectedResponse(response.status_code, response.text)
            body = response.json()
            self.metrics["pages_read"] += 1
            yield from body["data"]
            cursor = body.get("next_cursor")
            if not cursor:
                return

    def list_payees(self) -> list[dict]:
        return list(self.iter_pages("/v1/payees"))

    def iter_payouts(self, period: str | None = None) -> Iterator[dict]:
        return self.iter_pages("/v1/payouts", params={"period": period} if period else None)

    def find_payouts(self, external_ids: Iterable[str]) -> dict[str, list[dict]]:
        """Payouts the destination already holds for these external ids."""
        found: dict[str, list[dict]] = defaultdict(list)
        ids = sorted(set(external_ids))
        for start in range(0, len(ids), MAX_IDS_PER_QUERY):
            chunk = ids[start : start + MAX_IDS_PER_QUERY]
            for payout in self.iter_pages("/v1/payouts", params={"external_id": chunk}):
                found[payout["external_id"]].append(payout)
        return dict(found)

    def create_payout_batch(self, lines: list[dict], idempotency_key: str) -> httpx.Response:
        return self.request("POST", "/v1/payout-batches", json={"lines": lines}, idempotency_key=idempotency_key)
