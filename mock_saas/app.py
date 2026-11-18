"""Mock payouts SaaS: the downstream system Puente has to survive.

It behaves like a vendor API on a bad day:

* OAuth2 client credentials, with access tokens that expire.
* A token-bucket rate limit that answers 429 with Retry-After.
* Cursor pagination on every list.
* Strict validation that says which lines of a batch are wrong.
* Idempotency-Key on writes, and a 409 while the first request is still running.
* Failure injection: transient 5xx, answers lost after a write, slow answers,
  revoked tokens and a full outage (see ChaosConfig).

Run it with ``uvicorn mock_saas.app:app --port 8000``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import random
import secrets
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import ChaosConfig, ServerConfig
from .models import PAYOUT_CAPS, PayoutBatchIn
from .ratelimit import TokenBucket
from .seed import seed_payees

MAX_PAGE_SIZE = 100
MAX_ID_FILTERS = 100
BEARER_CHALLENGE = {"WWW-Authenticate": 'Bearer error="invalid_token"'}


def error(status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra) -> JSONResponse:
    return JSONResponse({"error": code, "message": message, **extra}, status_code=status, headers=headers)


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"offset:{offset}".encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        kind, value = base64.urlsafe_b64decode(cursor.encode()).decode().split(":", 1)
        offset = int(value)
    except ValueError as exc:  # bad base64, bad text or a missing separator
        raise ValueError("invalid cursor") from exc
    if kind != "offset" or offset < 0:
        raise ValueError("invalid cursor")
    return offset


def paginate(items: list[dict], cursor: str | None, limit: int) -> dict:
    offset = decode_cursor(cursor)
    end = offset + max(1, min(limit, MAX_PAGE_SIZE))
    return {"data": items[offset:end], "next_cursor": encode_cursor(end) if end < len(items) else None}


def schema_errors(exc: ValidationError, payload: object) -> list[dict]:
    """Group validation errors by line, so the client knows which lines to pull out."""
    by_index: dict[int, list[str]] = {}
    for err in exc.errors():
        loc = err.get("loc", ())
        if len(loc) >= 2 and loc[0] == "lines" and isinstance(loc[1], int):
            field = ".".join(str(part) for part in loc[2:]) or "line"
            by_index.setdefault(loc[1], []).append(f"{field}: {err['msg']}")
    lines = payload.get("lines") if isinstance(payload, dict) else None
    result = []
    for index, messages in sorted(by_index.items()):
        line = lines[index] if isinstance(lines, list) and index < len(lines) else None
        result.append({"index": index, "external_id": line.get("external_id") if isinstance(line, dict) else None, "errors": messages})
    return result


class MockState:
    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self.payees = seed_payees(config.payee_count)
        self.payees_by_id = {payee["payee_id"]: payee for payee in self.payees}
        self.tokens: dict[str, tuple[str, float]] = {}
        self.set_chaos(config.chaos)
        self.reset()

    def set_chaos(self, chaos: ChaosConfig) -> None:
        self.chaos = chaos
        self.rng = random.Random(chaos.seed)
        self.batch_requests = 0
        self.outage_until = 0.0

    def reset(self) -> None:
        self.payouts: list[dict] = []
        self.idempotency: dict[str, dict] = {}
        self.in_flight: set[str] = set()
        self.buckets: dict[str, TokenBucket] = {}
        self.stats: Counter[str] = Counter()

    def roll(self, rate: float) -> bool:
        return rate > 0 and self.rng.random() < rate


def create_app(config: ServerConfig | None = None) -> FastAPI:
    config = config or ServerConfig.from_env()
    state = MockState(config)
    app = FastAPI(title="Mock payouts SaaS", version="1.0.0")
    app.state.mock = state

    # -- checks shared by every call ------------------------------------

    def in_outage() -> bool:
        return time.monotonic() < state.outage_until

    def outage_answer() -> JSONResponse:
        state.stats["outage_rejections"] += 1
        return error(503, "service_unavailable", "The service is down", headers={"Retry-After": "2"})

    def authenticate(request: Request) -> tuple[str | None, JSONResponse | None]:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        token = token.strip()
        entry = state.tokens.get(token) if scheme.lower() == "bearer" else None
        if entry is None or entry[1] <= time.monotonic():
            state.stats["auth_failures"] += 1
            return None, error(401, "invalid_token", "Missing, unknown or expired token", headers=BEARER_CHALLENGE)
        if state.roll(state.chaos.auth_error_rate):
            state.tokens.pop(token, None)
            state.stats["injected_auth_errors"] += 1
            return None, error(401, "invalid_token", "Token was revoked", headers=BEARER_CHALLENGE)
        return entry[0], None

    def rate_limited(client_id: str) -> JSONResponse | None:
        if not config.rate_limit_enabled:
            return None
        bucket = state.buckets.get(client_id)
        if bucket is None:
            bucket = state.buckets[client_id] = TokenBucket(config.rate_limit_capacity, config.rate_limit_refill_per_s)
        wait = bucket.take()
        if wait <= 0:
            return None
        state.stats["rate_limited"] += 1
        return error(429, "rate_limited", "Too many requests", headers={"Retry-After": str(max(1, math.ceil(wait)))})

    def gate(request: Request) -> tuple[str | None, JSONResponse | None]:
        """Outage, then auth, then rate limit, then random transient errors."""
        state.stats["requests"] += 1
        if in_outage():
            return None, outage_answer()
        client_id, denied = authenticate(request)
        if denied:
            return None, denied
        denied = rate_limited(client_id)
        if denied:
            return None, denied
        if state.roll(state.chaos.error_rate):
            state.stats["injected_errors"] += 1
            return None, error(state.rng.choice((500, 502, 503)), "transient_error", "Injected transient failure")
        return client_id, None

    # -- validation and writes -------------------------------------------

    def business_errors(batch: PayoutBatchIn) -> list[dict]:
        problems = []
        seen: set[str] = set()
        for index, line in enumerate(batch.lines):
            messages = []
            if line.external_id in seen:
                messages.append("external_id: repeated inside this batch")
            seen.add(line.external_id)
            payee = state.payees_by_id.get(line.payee_id)
            if payee is None:
                messages.append(f"payee_id: unknown payee {line.payee_id}")
            elif payee["status"] != "active":
                messages.append("payee_id: payee is not active")
            elif payee["currency"] != line.currency:
                messages.append(f"currency: payee {line.payee_id} is paid in {payee['currency']}")
            cap = PAYOUT_CAPS[line.currency]
            if line.amount > cap:
                messages.append(f"amount: {line.amount} is over the per-line cap of {cap} {line.currency}")
            if messages:
                problems.append({"index": index, "external_id": line.external_id, "errors": messages})
        return problems

    def validate(raw: bytes) -> tuple[PayoutBatchIn | None, JSONResponse | None]:
        try:
            payload = json.loads(raw)
        except ValueError:
            return None, error(400, "invalid_json", "Body is not valid JSON")
        try:
            batch = PayoutBatchIn.model_validate(payload)
        except ValidationError as exc:
            return None, error(422, "validation_error", "Payload does not match the schema", invalid_lines=schema_errors(exc, payload))
        if len(batch.lines) > config.max_batch_lines:
            return None, error(422, "batch_too_large", f"At most {config.max_batch_lines} lines per batch")
        problems = business_errors(batch)
        if problems:
            return None, error(422, "validation_error", "Some lines were rejected", invalid_lines=problems)
        return batch, None

    def commit(batch: PayoutBatchIn) -> dict:
        batch_id = f"bat_{uuid.uuid4().hex[:16]}"
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for line in batch.lines:
            state.payouts.append(
                {
                    "payout_id": f"po_{uuid.uuid4().hex[:16]}",
                    "batch_id": batch_id,
                    "external_id": line.external_id,
                    "payee_id": line.payee_id,
                    "period": line.period,
                    "currency": line.currency,
                    "amount": f"{line.amount:.2f}",
                    "memo": line.memo,
                    "created_at": created_at,
                }
            )
        state.stats["batches_committed"] += 1
        state.stats["lines_committed"] += len(batch.lines)
        return {"batch_id": batch_id, "status": "accepted", "lines": len(batch.lines)}

    # -- endpoints -------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/oauth/token")
    async def issue_token(request: Request):
        if in_outage():
            return outage_answer()
        if state.roll(state.chaos.error_rate):
            state.stats["injected_errors"] += 1
            return error(503, "transient_error", "Injected transient failure")
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode("utf-8", "replace")).items()}
        if form.get("grant_type") != "client_credentials":
            return error(400, "unsupported_grant_type", "Only client_credentials is supported")
        client_ok = secrets.compare_digest(form.get("client_id", "").encode(), config.client_id.encode())
        secret_ok = secrets.compare_digest(form.get("client_secret", "").encode(), config.client_secret.encode())
        if not (client_ok and secret_ok):
            return error(401, "invalid_client", "Unknown client or wrong secret")
        now = time.monotonic()
        for token, (_, expires_at) in list(state.tokens.items()):
            if expires_at <= now:
                del state.tokens[token]
        token = secrets.token_urlsafe(24)
        state.tokens[token] = (config.client_id, now + config.token_ttl_seconds)
        state.stats["tokens_issued"] += 1
        return {"access_token": token, "token_type": "Bearer", "expires_in": config.token_ttl_seconds}

    @app.get("/v1/payees")
    async def list_payees(request: Request, cursor: str | None = None, limit: int = MAX_PAGE_SIZE):
        _, denied = gate(request)
        if denied:
            return denied
        try:
            return paginate(state.payees, cursor, limit)
        except ValueError:
            return error(400, "invalid_cursor", "Cursor is not valid")

    @app.get("/v1/payouts")
    async def list_payouts(request: Request, cursor: str | None = None, limit: int = MAX_PAGE_SIZE, period: str | None = None):
        _, denied = gate(request)
        if denied:
            return denied
        wanted = request.query_params.getlist("external_id")
        if len(wanted) > MAX_ID_FILTERS:
            return error(400, "too_many_filters", f"At most {MAX_ID_FILTERS} external_id filters per call")
        items = state.payouts
        if period:
            items = [payout for payout in items if payout["period"] == period]
        if wanted:
            keys = set(wanted)
            items = [payout for payout in items if payout["external_id"] in keys]
        try:
            return paginate(items, cursor, limit)
        except ValueError:
            return error(400, "invalid_cursor", "Cursor is not valid")

    @app.post("/v1/payout-batches")
    async def create_payout_batch(request: Request):
        chaos = state.chaos
        state.batch_requests += 1
        state.stats["batch_requests"] += 1
        if chaos.outage_at_request is not None and state.batch_requests == chaos.outage_at_request:
            state.outage_until = time.monotonic() + chaos.outage_seconds
            state.stats["outages_started"] += 1
        _, denied = gate(request)
        if denied:
            return denied

        key = request.headers.get("idempotency-key", "").strip()
        if not key or len(key) > 255:
            return error(400, "missing_idempotency_key", "Send an Idempotency-Key header of 1 to 255 characters")
        raw = await request.body()
        fingerprint = hashlib.sha256(raw).hexdigest()

        stored = state.idempotency.get(key)
        if stored is not None:
            if stored["fingerprint"] != fingerprint:
                state.stats["idempotency_mismatches"] += 1
                return error(422, "idempotency_key_reused", "This Idempotency-Key was already used with another payload")
            state.stats["idempotent_replays"] += 1
            return JSONResponse(stored["body"], status_code=stored["status"], headers={"Idempotent-Replayed": "true"})
        if key in state.in_flight:
            state.stats["in_flight_conflicts"] += 1
            return error(409, "request_in_progress", "A request with this Idempotency-Key is still running", headers={"Retry-After": "1"})

        slow = state.roll(chaos.slow_rate)
        slow_before_write = slow and state.rng.random() < 0.5
        ghost = state.roll(chaos.ghost_write_rate)

        state.in_flight.add(key)
        try:
            if slow_before_write:
                state.stats["injected_slow"] += 1
                await asyncio.sleep(chaos.slow_seconds)
            batch, rejected = validate(raw)
            if rejected is not None:
                state.stats["batches_rejected"] += 1
                return rejected
            body = commit(batch)
            state.idempotency[key] = {"fingerprint": fingerprint, "status": 201, "body": body}
        finally:
            state.in_flight.discard(key)

        if ghost:
            state.stats["injected_ghost_writes"] += 1
            return error(504, "gateway_timeout", "Upstream timed out")
        if slow and not slow_before_write:
            state.stats["injected_slow"] += 1
            await asyncio.sleep(chaos.slow_seconds)
        return JSONResponse(body, status_code=201)

    # -- admin, for tests and the benchmark ------------------------------

    if config.admin_enabled:

        @app.get("/_admin/chaos")
        async def read_chaos():
            return state.chaos.model_dump()

        @app.put("/_admin/chaos")
        async def write_chaos(request: Request):
            try:
                chaos = ChaosConfig.model_validate_json((await request.body()) or b"{}")
            except ValidationError as exc:
                return error(422, "invalid_chaos", "Chaos settings are not valid", details=json.loads(exc.json()))
            state.set_chaos(chaos)
            return chaos.model_dump()

        @app.post("/_admin/reset")
        async def reset():
            state.reset()
            return {"status": "reset"}

        @app.get("/_admin/stats")
        async def stats():
            return {"stats": dict(state.stats), "payouts": len(state.payouts), "idempotency_keys": len(state.idempotency)}

    return app


app = create_app()
