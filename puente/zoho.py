"""Zoho CRM client: refresh-token OAuth, retries, pagination, batched upsert and a credit budget.

What Zoho CRM (API v8) does that a client has to plan for:

* Access tokens last an hour and come from a refresh token that does not
  expire. The token answer also names the data center to call (api_domain).
* The accounts server reports a bad code or client as HTTP 200 with an "error"
  key, so the body decides, not the status.
* Every call spends credits from a pool the whole organization shares for 24
  hours: 1 per read of up to 200 records, 1 per 10 records written, 10 per
  custom field created. Puente gives each run its own budget, and once Zoho
  starts reporting the pool (X-API-CREDITS-REMAINING, sent after half of it is
  gone) it stops before the pool drops below a reserve the other integrations
  of the organization need.
* Upsert writes up to 100 records and answers each one in input order, with 207
  when only some made it. It matches records on a unique field, so sending a
  batch again after a lost answer updates the records it wrote instead of
  creating them twice. That is what makes retrying a write safe here.
* GET records answers 204, with no body, when there is nothing to read.
"""

from __future__ import annotations

import logging
import math
import time
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import httpx

from .errors import AuthError, PuenteError, RetryExhausted
from .retry import RETRYABLE_EXCEPTIONS, RETRYABLE_STATUS, RetryPolicy, parse_retry_after

log = logging.getLogger("puente.zoho")

DEFAULT_ACCOUNTS_URL = "https://accounts.zoho.com"
API_VERSION = "v8"
MAX_UPSERT_RECORDS = 100
MAX_PAGE_SIZE = 200
MAX_DISCRETE_PAGES = 10  # past 2,000 records, only page_token goes on
MAX_FIELDS_PER_READ = 50
MAX_FIELDS_PER_CREATE = 5
MAX_TOKEN_REFRESHES_PER_CALL = 2
READ_CREDITS = 1
FIELD_CREATE_CREDITS = 10
CREDITS_HEADER = "X-API-CREDITS-REMAINING"

# What to ask for when generating the grant code: the two modules Puente writes,
# the field settings it creates, and the org it names before writing anything.
SCOPES = (
    "ZohoCRM.modules.accounts.ALL",
    "ZohoCRM.modules.contacts.ALL",
    "ZohoCRM.settings.fields.ALL",
    "ZohoCRM.org.READ",
)

TOKEN_ERRORS = {
    "invalid_client": (
        "Zoho does not know this client ID and secret. Check ZOHO_CLIENT_ID and ZOHO_CLIENT_SECRET, and that "
        "ZOHO_ACCOUNTS_URL is the data center of your account (accounts.zoho.com, accounts.zoho.eu, ...)"
    ),
    "invalid_code": (
        "the grant code or refresh token is wrong, expired or already used. A grant code works once and only for "
        "the minutes you chose: generate a new one and run `python -m puente zoho auth` again"
    ),
}


class ZohoError(PuenteError):
    """Zoho refused a call in a way that retrying will not fix."""

    def __init__(self, what: str, status_code: int, code: str | None, message: str | None, details: dict | None = None) -> None:
        text = f"{what}: HTTP {status_code}"
        if code:
            text += f" {code}"
        if message:
            text += f": {message}"
        if details:
            text += f" {details}"
        super().__init__(text)
        self.status_code = status_code
        self.code = code
        self.details = details or {}

    @classmethod
    def from_response(cls, what: str, response: httpx.Response) -> ZohoError:
        body = body_of(response)
        details = body.get("details") if isinstance(body.get("details"), dict) else None
        return cls(what, response.status_code, body.get("code"), body.get("message") or response.text[:200], details)


class CreditBudgetExceeded(PuenteError):
    """The next call would spend more Zoho API credits than this run may."""


def body_of(response: httpx.Response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def upsert_credits(records: int) -> int:
    """1 credit for every 10 records inserted, updated or upserted."""
    return max(1, math.ceil(records / 10))


@dataclass
class CreditMeter:
    """Puente's own limit on the organization's daily API credits.

    budget is the most one run may spend. reserve is what must stay in the
    organization's pool for everyone else; it applies once Zoho reports the pool.
    """

    budget: int = 500
    reserve: int = 0
    used: int = 0
    remaining: int | None = None

    def charge(self, credits: int, what: str) -> None:
        self.ensure(credits, what)
        self.used += credits
        if self.remaining is not None:
            self.remaining -= credits  # an estimate until the next answer reports the real figure

    def ensure(self, credits: int, what: str) -> None:
        """Raise if spending this much now would break the budget or the reserve."""
        if self.used + credits > self.budget:
            raise CreditBudgetExceeded(
                f"{what} needs {credits} credits and this run has used {self.used} of its {self.budget} (PUENTE_ZOHO_CREDIT_BUDGET)"
            )
        if self.remaining is not None and self.remaining - credits < self.reserve:
            raise CreditBudgetExceeded(
                f"{what} needs {credits} credits, Zoho has {self.remaining} left for the organization today "
                f"and Puente keeps {self.reserve} for everyone else (PUENTE_ZOHO_CREDIT_RESERVE)"
            )

    def observe(self, response: httpx.Response) -> None:
        value = response.headers.get(CREDITS_HEADER, "").strip()
        if value.isdigit():
            self.remaining = int(value)


@dataclass(frozen=True)
class UpsertResult:
    key: str
    ok: bool
    action: str | None = None  # insert or update
    record_id: str | None = None
    code: str = ""
    message: str = ""
    field: str | None = None  # the field Zoho complained about


def read_upsert_item(key: str, item: object) -> UpsertResult:
    if not isinstance(item, dict):
        return UpsertResult(key, False, code="UNREADABLE", message=str(item)[:200])
    details = item.get("details") if isinstance(item.get("details"), dict) else {}
    if item.get("status") == "success":
        record_id = details.get("id")
        return UpsertResult(key, True, item.get("action"), str(record_id) if record_id else None, item.get("code", "SUCCESS"), item.get("message", ""))
    return UpsertResult(key, False, code=item.get("code", ""), message=item.get("message", ""), field=details.get("api_name"))


class ZohoClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str = "",
        *,
        accounts_url: str = DEFAULT_ACCOUNTS_URL,
        api_domain: str = "",
        timeout_s: float = 15.0,
        retry: RetryPolicy | None = None,
        credits: CreditMeter | None = None,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http or httpx.Client(timeout=timeout_s)
        self._owns_http = http is None
        self._client_id = client_id
        self._client_secret = client_secret
        self._refresh_token = refresh_token
        self._accounts_url = accounts_url.rstrip("/")
        self._api_domain = api_domain.rstrip("/")
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._clock = clock
        self._token: str | None = None
        self._token_expires_at = 0.0
        self.credits = credits or CreditMeter()
        self.metrics: Counter[str] = Counter()

    def __repr__(self) -> str:  # never the secrets
        return f"ZohoClient(accounts_url={self._accounts_url!r}, api_domain={self._api_domain!r})"

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> ZohoClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @property
    def api_domain(self) -> str:
        return self._api_domain

    # -- OAuth ------------------------------------------------------------------

    def exchange_grant_code(self, code: str) -> dict:
        """Trade a Self Client grant code for a refresh token. The code works once, for a few minutes."""
        body = self._token_request({"grant_type": "authorization_code", "code": code})
        if not body.get("refresh_token"):
            raise AuthError("Zoho returned no refresh token for this grant code")
        self._refresh_token = body["refresh_token"]
        return body

    def _access_token(self) -> str:
        if self._token and self._clock() < self._token_expires_at:
            return self._token
        if not self._refresh_token:
            raise AuthError("no Zoho refresh token: run `python -m puente zoho auth` first")
        self._token_request({"grant_type": "refresh_token", "refresh_token": self._refresh_token})
        return self._token or ""

    def _token_request(self, form: dict[str, str]) -> dict:
        # The secrets travel in the form body, never in the URL, where proxies and logs keep them.
        data = {**form, "client_id": self._client_id, "client_secret": self._client_secret}
        response = self._send("POST", f"{self._accounts_url}/oauth/v2/token", data=data, authenticated=False, what="Zoho token request")
        body = body_of(response)
        if response.status_code != 200 or "error" in body or not body.get("access_token"):
            code = str(body.get("error") or f"HTTP {response.status_code}")
            raise AuthError(f"Zoho refused the token request ({code}): {TOKEN_ERRORS.get(code, 'check the Self Client in api-console.zoho.com')}")
        ttl = float(body.get("expires_in") or 3600)
        self._token = body["access_token"]
        # Renew a little early, so a token never expires halfway through a call.
        self._token_expires_at = self._clock() + ttl - min(60.0, ttl * 0.1)
        if body.get("api_domain"):
            self._api_domain = str(body["api_domain"]).rstrip("/")
        self.metrics["token_fetches"] += 1
        return body

    # -- calls with retries -----------------------------------------------------

    def _url(self, path: str) -> str:
        self._access_token()  # the token answer says which data center to call
        if not self._api_domain:
            raise PuenteError("Zoho did not say which API domain to call: set ZOHO_API_DOMAIN in .env")
        return f"{self._api_domain}/crm/{API_VERSION}{path}"

    def _send(self, method: str, url: str, *, params=None, json=None, data=None, authenticated: bool = True, credits: int = 0, what: str = "") -> httpx.Response:
        what = what or f"{method} {url}"
        started = self._clock()
        attempt = 0
        refreshes = 0
        while True:
            attempt += 1
            headers = {"Authorization": f"Zoho-oauthtoken {self._access_token()}"} if authenticated else {}
            if credits:
                self.credits.charge(credits, what)  # every attempt, because a failed one may cost too
            retry_after = None
            try:
                response = self._http.request(method, url, params=params, json=json, data=data, headers=headers)
            except RETRYABLE_EXCEPTIONS as exc:
                outcome = type(exc).__name__
            else:
                self.credits.observe(response)
                if response.status_code == 401 and authenticated:
                    code = body_of(response).get("code")
                    if code == "OAUTH_SCOPE_MISMATCH":
                        raise AuthError(f"{what}: the refresh token lacks a scope this call needs. Run `python -m puente zoho auth` again and paste the scopes it prints")
                    if refreshes >= MAX_TOKEN_REFRESHES_PER_CALL:
                        raise AuthError(f"{what}: Zoho refused the access token again after {refreshes} refreshes ({code})")
                    refreshes += 1
                    attempt -= 1  # a refresh is not a failed attempt
                    self._token = None
                    self.metrics["token_refused"] += 1
                    log.warning("zoho_token_refused", extra={"what": what, "refresh": refreshes, "code": code})
                    continue
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                outcome = f"HTTP {response.status_code}"
                retry_after = parse_retry_after(response.headers.get("Retry-After"))

            if attempt >= self._retry.max_attempts:
                raise RetryExhausted(f"{what}: {attempt} attempts, last {outcome}", attempts=attempt, last_outcome=outcome)
            wait = self._retry.delay(attempt - 1, retry_after)
            if self._clock() - started + wait > self._retry.max_elapsed_s:
                raise RetryExhausted(
                    f"{what}: retry budget of {self._retry.max_elapsed_s:g} s spent after {attempt} attempts, last {outcome}",
                    attempts=attempt,
                    last_outcome=outcome,
                )
            self.metrics["retries"] += 1
            self.metrics[f"retry:{outcome}"] += 1
            log.info("zoho_retrying", extra={"what": what, "attempt": attempt, "outcome": outcome, "wait_s": round(wait, 3)})
            self._sleep(wait)

    def _expect(self, response: httpx.Response, what: str, ok: tuple[int, ...] = (200,)) -> dict:
        body = body_of(response)
        if response.status_code not in ok or not body:
            raise ZohoError.from_response(what, response)
        return body

    # -- API --------------------------------------------------------------------

    def get_org(self) -> dict:
        what = "read the org"
        body = self._expect(self._send("GET", self._url("/org"), credits=READ_CREDITS, what=what), what)
        orgs = body.get("org")
        return orgs[0] if isinstance(orgs, list) and orgs and isinstance(orgs[0], dict) else {}

    def get_fields(self, module: str) -> dict[str, dict]:
        """A module's field metadata, by API name."""
        what = f"read the {module} fields"
        response = self._send("GET", self._url("/settings/fields"), params={"module": module}, credits=READ_CREDITS, what=what)
        fields = self._expect(response, what).get("fields") or []
        return {item["api_name"]: item for item in fields if isinstance(item, dict) and item.get("api_name")}

    def create_fields(self, module: str, specs: list[dict]) -> list[dict]:
        """Create custom fields, 5 per call. One result per field, in input order."""
        results: list[dict] = []
        for start in range(0, len(specs), MAX_FIELDS_PER_CREATE):
            chunk = specs[start : start + MAX_FIELDS_PER_CREATE]
            what = f"create {len(chunk)} {module} fields"
            response = self._send(
                "POST", self._url("/settings/fields"), params={"module": module}, json={"fields": chunk}, credits=FIELD_CREATE_CREDITS * len(chunk), what=what
            )
            items = body_of(response).get("fields")
            if not isinstance(items, list) or len(items) != len(chunk):
                raise ZohoError.from_response(what, response)
            results.extend(item if isinstance(item, dict) else {"status": "error", "message": str(item)} for item in items)
        return results

    def iter_records(self, module: str, fields: list[str] | tuple[str, ...], *, per_page: int = MAX_PAGE_SIZE) -> Iterator[dict]:
        """Every record of a module, page after page, with only the fields asked for."""
        if not 0 < len(fields) <= MAX_FIELDS_PER_READ:
            raise ValueError(f"ask for 1 to {MAX_FIELDS_PER_READ} fields")
        size = max(1, min(per_page, MAX_PAGE_SIZE))
        what = f"read {module}"
        page, token = 1, None
        while True:
            params: dict[str, object] = {"fields": ",".join(fields), "per_page": size}
            if token:
                params["page_token"] = token  # never together with page
            else:
                params["page"] = page
            response = self._send("GET", self._url(f"/{module}"), params=params, credits=READ_CREDITS, what=what)
            if response.status_code == 204:
                return  # nothing (more) to read
            body = self._expect(response, what)
            self.metrics["pages_read"] += 1
            yield from body.get("data") or []
            info = body.get("info") or {}
            if not info.get("more_records"):
                return
            next_token = info.get("next_page_token")
            page += 1
            if next_token:
                token = next_token
            elif token is not None or page > MAX_DISCRETE_PAGES:
                raise ZohoError(what, response.status_code, "NO_PAGE_TOKEN", "Zoho says there are more records but gave no page_token to read them")

    def upsert(self, module: str, records: list[dict], *, key_field: str) -> list[UpsertResult]:
        """Insert or update records matched on a unique field, 100 per call. One result per record, in order.

        Workflows, approvals and blueprints are not triggered (trigger: []), so loading
        data does not send emails or start processes in the CRM.
        """
        results: list[UpsertResult] = []
        chunks = [records[start : start + MAX_UPSERT_RECORDS] for start in range(0, len(records), MAX_UPSERT_RECORDS)]
        # Check the whole load first, so a budget that is too small stops it before half of it is written.
        self.credits.ensure(sum(upsert_credits(len(chunk)) for chunk in chunks), f"upsert {len(records)} {module}")
        for chunk in chunks:
            what = f"upsert {len(chunk)} {module}"
            payload = {"data": chunk, "duplicate_check_fields": [key_field], "trigger": []}
            response = self._send("POST", self._url(f"/{module}/upsert"), json=payload, credits=upsert_credits(len(chunk)), what=what)
            items = body_of(response).get("data")
            if not isinstance(items, list) or len(items) != len(chunk):
                raise ZohoError.from_response(what, response)  # the whole call was refused
            results.extend(read_upsert_item(str(record.get(key_field) or ""), item) for record, item in zip(chunk, items))
            self.metrics["records_written"] += len(chunk)
        return results
