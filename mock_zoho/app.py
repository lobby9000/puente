"""Fake Zoho CRM: the accounts server and the part of CRM API v8 that Puente uses.

It copies the documented behaviour a client has to get right:

* OAuth for a Self Client: a grant code works once and buys a refresh token,
  the refresh token buys access tokens that expire, and the accounts server
  reports a bad code or client as HTTP 200 with an "error" key.
* Calls carry "Authorization: Zoho-oauthtoken <token>". An expired token gets
  401 INVALID_TOKEN, a token without the right scope 401 OAUTH_SCOPE_MISMATCH.
* GET records needs a fields list, returns at most 200 per page, pages with
  page (up to 2,000 records) or page_token, and answers 204 when there is nothing.
* Upsert takes at most 100 records, matches them on a unique field, answers each
  one in input order, and returns 207 when only some succeeded.
* Field metadata: list a module's fields and create custom ones, 5 per call.
* Credits: every call costs what the API limits page says (1 per read, 1 per 10
  records written, 10 per field created) from a daily pool, and once half of the
  pool is gone answers carry X-API-CREDITS-REMAINING.
* Chaos: 429s, 500s, upserts whose answer is lost after they were saved, and
  tokens revoked before they expire (see ZohoChaos).

Run it with ``uvicorn mock_zoho.app:app --port 8001``.
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
import secrets
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import parse_qs

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

API = "/crm/v8"
MAX_UPSERT = 100
MAX_PER_PAGE = 200
MAX_DISCRETE_PAGE = 10
MAX_FIELDS_PER_READ = 50
MAX_FIELDS_PER_CREATE = 5
MAX_UNIQUE_FIELDS = 2
ID_BASE = 5725767000000000000
FIELD_TYPES = ("text", "boolean", "email", "integer")
DEFAULT_SCOPES = (
    "ZohoCRM.modules.accounts.ALL",
    "ZohoCRM.modules.contacts.ALL",
    "ZohoCRM.settings.fields.ALL",
    "ZohoCRM.org.READ",
)


@dataclass(frozen=True)
class ZohoChaos:
    throttle_rate: float = 0.0  # 429 TOO_MANY_REQUESTS before doing anything
    error_rate: float = 0.0  # 500 INTERNAL_ERROR before doing anything
    lost_write_rate: float = 0.0  # an upsert is saved, then its answer is lost (500)
    token_revoke_rate: float = 0.0  # 401 INVALID_TOKEN for a token that had not expired
    seed: int | None = None


@dataclass(frozen=True)
class ZohoConfig:
    client_id: str = "fake-client-id"
    client_secret: str = "fake-client-secret"
    grant_code: str = "fake-grant-code"
    refresh_token: str = "fake-refresh-token"  # issued in advance, so an offline demo can skip the grant step
    api_domain: str = "http://localhost:8001"
    token_ttl_seconds: int = 3600
    daily_credits: int = 15000
    scopes: tuple[str, ...] = DEFAULT_SCOPES
    page_token_from_page: int = 1  # pages before this one carry no next_page_token
    company_name: str = "Puente demo org (fake Zoho)"
    chaos: ZohoChaos = field(default_factory=ZohoChaos)

    @classmethod
    def from_env(cls) -> ZohoConfig:
        env = os.environ.get

        def number(name: str, default: float) -> float:
            value = env(f"MOCK_ZOHO_{name}")
            return default if value in (None, "") else float(value)

        seed = env("MOCK_ZOHO_SEED")
        return cls(
            client_id=env("MOCK_ZOHO_CLIENT_ID", cls.client_id),
            client_secret=env("MOCK_ZOHO_CLIENT_SECRET", cls.client_secret),
            grant_code=env("MOCK_ZOHO_GRANT_CODE", cls.grant_code),
            refresh_token=env("MOCK_ZOHO_REFRESH_TOKEN", cls.refresh_token),
            api_domain=env("MOCK_ZOHO_API_DOMAIN", cls.api_domain),
            token_ttl_seconds=int(number("TOKEN_TTL_SECONDS", cls.token_ttl_seconds)),
            daily_credits=int(number("DAILY_CREDITS", cls.daily_credits)),
            chaos=ZohoChaos(
                throttle_rate=number("THROTTLE_RATE", 0.0),
                error_rate=number("ERROR_RATE", 0.0),
                lost_write_rate=number("LOST_WRITE_RATE", 0.0),
                token_revoke_rate=number("TOKEN_REVOKE_RATE", 0.0),
                seed=int(seed) if seed else None,
            ),
        )


def system_fields() -> dict[str, dict[str, dict]]:
    """The standard fields a new org already has, as far as Puente touches them."""
    text = "text"
    return {
        "Accounts": {
            "Account_Name": {"field_label": "Account Name", "data_type": text, "length": 200, "system_mandatory": True},
            "Billing_Country": {"field_label": "Billing Country", "data_type": text, "length": 100},
        },
        "Contacts": {
            "First_Name": {"field_label": "First Name", "data_type": text, "length": 40},
            "Last_Name": {"field_label": "Last Name", "data_type": text, "length": 80, "system_mandatory": True},
            "Email": {"field_label": "Email", "data_type": "email", "length": 100},
            "Account_Name": {"field_label": "Account Name", "data_type": "lookup", "lookup": "Accounts"},
        },
    }


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def encode_page_token(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode()


def decode_page_token(token: str) -> int:
    try:
        offset = json.loads(base64.urlsafe_b64decode(token.encode()).decode())["offset"]
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("invalid page token") from exc
    if not isinstance(offset, int) or offset < 0:
        raise ValueError("invalid page token")
    return offset


def zoho_error(status: int, code: str, message: str, details: dict | None = None, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"code": code, "details": details or {}, "message": message, "status": "error"}, status_code=status, headers=headers)


def record_error(code: str, message: str, **details) -> dict:
    return {"code": code, "details": details, "message": message, "status": "error"}


class Module:
    def __init__(self, name: str, fields: dict[str, dict]) -> None:
        self.name = name
        self.fields = fields
        self.records: dict[str, dict] = {}

    def unique_fields(self) -> list[str]:
        return [api for api, meta in self.fields.items() if meta.get("unique")]

    def find(self, api_name: str, value: object) -> str | None:
        wanted = str(value).strip().lower()
        for record_id, record in self.records.items():
            current = record.get(api_name)
            if current is not None and str(current).strip().lower() == wanted:
                return record_id
        return None


class ZohoState:
    def __init__(self, config: ZohoConfig) -> None:
        self.config = config
        self.rng = random.Random(config.chaos.seed)
        self.modules = {name: Module(name, fields) for name, fields in system_fields().items()}
        self.access_tokens: dict[str, float] = {}
        self.refresh_tokens = {config.refresh_token}
        self.used_codes: set[str] = set()
        self.credits_used = 0
        self.last_id = 0
        self.stats: Counter[str] = Counter()

    def new_id(self) -> str:
        self.last_id += 1
        return str(ID_BASE + self.last_id)

    def roll(self, rate: float) -> bool:
        return rate > 0 and self.rng.random() < rate


def check_value(module: dict[str, Module], meta: dict, value: object) -> str | None:
    """None when the value fits the field, otherwise why it does not."""
    if value is None:
        return None
    kind = meta["data_type"]
    if kind == "boolean":
        return None if isinstance(value, bool) else "expected true or false"
    if kind == "lookup":
        target = module.get(meta.get("lookup", ""))
        if not isinstance(value, dict) or target is None or str(value.get("id")) not in target.records:
            return "the record this lookup points to does not exist"
        return None
    if kind == "integer":
        return None if isinstance(value, int) and not isinstance(value, bool) else "expected an integer"
    if not isinstance(value, str):
        return "expected text"
    if meta.get("length") and len(value) > meta["length"]:
        return f"longer than {meta['length']} characters"
    if kind == "email" and value and "@" not in value:
        return "not an email address"
    return None


def create_app(config: ZohoConfig | None = None) -> FastAPI:
    config = config or ZohoConfig.from_env()
    state = ZohoState(config)
    chaos = config.chaos
    app = FastAPI(title="Fake Zoho CRM", version="8.0-fake")
    app.state.zoho = state

    def credit_headers() -> dict[str, str]:
        if state.credits_used * 2 >= config.daily_credits:
            return {"X-API-CREDITS-REMAINING": str(max(0, config.daily_credits - state.credits_used))}
        return {}

    def answer(body: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers=credit_headers())

    def guard(request: Request, scope: str, credits: int) -> JSONResponse | None:
        """Authentication, scope, chaos and credits, in the order a real call meets them."""
        header = request.headers.get("authorization", "")
        prefix = "Zoho-oauthtoken "
        if not header.startswith(prefix):
            return zoho_error(401, "AUTHENTICATION_FAILURE", "Authentication failed")
        token = header[len(prefix) :]
        expires_at = state.access_tokens.get(token)
        if expires_at is None or expires_at <= time.monotonic():
            return zoho_error(401, "INVALID_TOKEN", "invalid oauth token")
        if scope not in config.scopes:
            return zoho_error(401, "OAUTH_SCOPE_MISMATCH", "invalid oauth scope to access this URL")
        if state.roll(chaos.token_revoke_rate):
            del state.access_tokens[token]
            state.stats["injected_token_revocations"] += 1
            return zoho_error(401, "INVALID_TOKEN", "invalid oauth token")
        if state.roll(chaos.throttle_rate):
            state.stats["injected_throttles"] += 1
            return zoho_error(429, "TOO_MANY_REQUESTS", "too many requests at the same time; try again later")
        if state.roll(chaos.error_rate):
            state.stats["injected_errors"] += 1
            return zoho_error(500, "INTERNAL_ERROR", "Internal Server Error")
        if state.credits_used + credits > config.daily_credits:
            state.stats["refused_for_credits"] += 1
            return zoho_error(429, "TOO_MANY_REQUESTS", "API credits for the last 24 hours are used up", headers=credit_headers())
        state.credits_used += credits
        state.stats["calls"] += 1
        return None

    async def read_json(request: Request) -> object:
        try:
            return json.loads(await request.body() or b"null")
        except ValueError:
            return None

    def module_or_error(name: str | None) -> Module | JSONResponse:
        module = state.modules.get(name or "")
        if module is None:
            return zoho_error(400, "INVALID_MODULE", "the module name given seems to be invalid", {"module": name})
        return module

    def project(module: Module, record_id: str, names: list[str]) -> dict:
        record = module.records[record_id]
        result = {"id": record_id}
        for name in names:
            result[name] = record.get(name)
        return result

    # -- accounts server --------------------------------------------------------

    def issue_access_token() -> dict:
        token = "1000." + secrets.token_hex(16)
        state.access_tokens[token] = time.monotonic() + config.token_ttl_seconds
        state.stats["access_tokens_issued"] += 1
        return {
            "access_token": token,
            "scope": " ".join(config.scopes),
            "api_domain": config.api_domain,
            "token_type": "Bearer",
            "expires_in": config.token_ttl_seconds,
        }

    @app.post("/oauth/v2/token")
    async def token(request: Request):
        form = {key: values[0] for key, values in parse_qs((await request.body()).decode()).items()}
        form = {**dict(request.query_params), **form}  # Zoho's own examples put these in the URL
        if form.get("client_id") != config.client_id or form.get("client_secret") != config.client_secret:
            return JSONResponse({"error": "invalid_client"})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            code = form.get("code")
            if code != config.grant_code or code in state.used_codes:
                return JSONResponse({"error": "invalid_code"})
            state.used_codes.add(code)
            refresh = "1000." + secrets.token_hex(20)
            state.refresh_tokens.add(refresh)
            return JSONResponse({**issue_access_token(), "refresh_token": refresh})
        if grant == "refresh_token":
            if form.get("refresh_token") not in state.refresh_tokens:
                return JSONResponse({"error": "invalid_code"})
            return JSONResponse(issue_access_token())
        return JSONResponse({"error": "unsupported_grant_type"})

    # -- CRM API ----------------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get(f"{API}/org")
    async def org(request: Request):
        if refusal := guard(request, "ZohoCRM.org.READ", 1):
            return refusal
        return answer({"org": [{"id": "fake-org-1", "company_name": config.company_name, "country_code": "MX", "currency": "MXN"}]})

    @app.get(f"{API}/settings/fields")
    async def list_fields(request: Request, module: str | None = None):
        if refusal := guard(request, "ZohoCRM.settings.fields.ALL", 1):
            return refusal
        target = module_or_error(module)
        if isinstance(target, JSONResponse):
            return target
        fields = []
        for api_name, meta in target.fields.items():
            fields.append(
                {
                    "api_name": api_name,
                    "field_label": meta["field_label"],
                    "data_type": meta["data_type"],
                    "length": meta.get("length"),
                    "custom_field": bool(meta.get("custom_field")),
                    "system_mandatory": bool(meta.get("system_mandatory")),
                    "unique": {"case_sensitive": False} if meta.get("unique") else None,
                    "id": meta.get("id", api_name),
                }
            )
        return answer({"fields": fields})

    @app.post(f"{API}/settings/fields")
    async def create_fields(request: Request, module: str | None = None):
        body = await read_json(request)
        specs = body.get("fields") if isinstance(body, dict) else None
        count = len(specs) if isinstance(specs, list) else 0
        if refusal := guard(request, "ZohoCRM.settings.fields.ALL", 10 * max(1, count)):
            return refusal
        target = module_or_error(module)
        if isinstance(target, JSONResponse):
            return target
        if not count:
            return zoho_error(400, "INVALID_DATA", "invalid data", {"api_name": "fields"})
        if count > MAX_FIELDS_PER_CREATE:
            return zoho_error(400, "LIMIT_EXCEEDED", f"at most {MAX_FIELDS_PER_CREATE} fields per call", {"limit": MAX_FIELDS_PER_CREATE})
        results = []
        for spec in specs:
            label = str(spec.get("field_label") or "").strip() if isinstance(spec, dict) else ""
            kind = spec.get("data_type") if isinstance(spec, dict) else None
            if not label or kind not in FIELD_TYPES:
                results.append(record_error("INVALID_DATA", "invalid data", api_name="data_type" if label else "field_label"))
                continue
            api_name = label.replace(" ", "_")
            if api_name in target.fields:
                results.append(record_error("DUPLICATE_DATA", "a field with this label already exists", api_name="field_label"))
                continue
            unique = bool(spec.get("unique"))
            if unique and len(target.unique_fields()) >= MAX_UNIQUE_FIELDS:
                results.append(record_error("LIMIT_EXCEEDED", f"a module can have at most {MAX_UNIQUE_FIELDS} unique fields", api_name="unique"))
                continue
            field_id = state.new_id()
            target.fields[api_name] = {
                "field_label": label,
                "data_type": kind,
                "length": int(spec.get("length") or 255) if kind == "text" else None,
                "custom_field": True,
                "unique": unique,
                "id": field_id,
            }
            state.stats["fields_created"] += 1
            results.append({"code": "SUCCESS", "details": {"id": field_id}, "message": "field created", "status": "success"})
        succeeded = sum(item["status"] == "success" for item in results)
        status = 201 if succeeded == len(results) else 400 if succeeded == 0 else 207
        return answer({"fields": results}, status)

    @app.get(f"{API}/{{module}}")
    async def get_records(
        request: Request,
        module: str,
        fields: str | None = None,
        per_page: int = MAX_PER_PAGE,
        page: int | None = None,
        page_token: str | None = None,
    ):
        if refusal := guard(request, f"ZohoCRM.modules.{module.lower()}.ALL", 1):
            return refusal
        target = module_or_error(module)
        if isinstance(target, JSONResponse):
            return target
        if not fields:
            return zoho_error(400, "REQUIRED_PARAM_MISSING", "One of the expected parameter is missing", {"param": "fields"})
        names = [name.strip() for name in fields.split(",") if name.strip()]
        if len(names) > MAX_FIELDS_PER_READ:
            return zoho_error(400, "LIMIT_EXCEEDED", f"at most {MAX_FIELDS_PER_READ} fields per call", {"param": "fields"})
        unknown = [name for name in names if name != "id" and name not in target.fields]
        if unknown:
            return zoho_error(400, "INVALID_DATA", "invalid data", {"api_name": unknown[0]})
        if page is not None and page_token:
            return zoho_error(400, "INVALID_DATA", "page and page_token cannot be used together", {"param": "page_token"})
        size = max(1, min(per_page, MAX_PER_PAGE))
        if page_token:
            try:
                offset = decode_page_token(page_token)
            except ValueError:
                return zoho_error(400, "INVALID_DATA", "invalid page token", {"param": "page_token"})
        else:
            page = page or 1
            if page > MAX_DISCRETE_PAGE:
                return zoho_error(400, "DISCRETE_PAGINATION_LIMIT_EXCEEDED", "use page_token to read past 2000 records", {"param": "page"})
            offset = (page - 1) * size
        ids = sorted(target.records, reverse=True)  # newest first, like the default sort by id
        chunk = ids[offset : offset + size]
        if not chunk:
            return Response(status_code=204, headers=credit_headers())
        number = offset // size + 1
        more = offset + size < len(ids)
        token_now = more and number >= config.page_token_from_page
        info = {
            "per_page": size,
            "count": len(chunk),
            "page": number,
            "sort_by": "id",
            "sort_order": "desc",
            "more_records": more,
            "next_page_token": encode_page_token(offset + size) if token_now else None,
            "previous_page_token": None,
            "page_token_expiry": None,
        }
        return answer({"data": [project(target, record_id, names) for record_id in chunk], "info": info})

    def upsert_one(target: Module, index: int, record: object, checks: list[str]) -> dict:
        if not isinstance(record, dict):
            return record_error("INVALID_DATA", "invalid data", json_path=f"$.data[{index}]")
        for name, value in record.items():
            meta = target.fields.get(name)
            if meta is None:
                return record_error("INVALID_DATA", "invalid data", api_name=name, json_path=f"$.data[{index}].{name}")
            problem = check_value(state.modules, meta, value)
            if problem:
                return record_error("INVALID_DATA", problem, api_name=name, json_path=f"$.data[{index}].{name}")
        existing, matched = None, None
        for name in checks:
            value = record.get(name)
            if value not in (None, ""):
                existing = target.find(name, value)
                if existing:
                    matched = name
                    break
        stamp = now_text()
        values = {}
        for name, value in record.items():
            if target.fields[name]["data_type"] == "lookup" and value is not None:
                linked = state.modules[target.fields[name]["lookup"]].records[str(value["id"])]
                value = {"id": str(value["id"]), "name": linked.get("Account_Name")}
            values[name] = value
        if existing:
            target.records[existing].update(values)
            target.records[existing]["Modified_Time"] = stamp
            state.stats["records_updated"] += 1
            return {
                "code": "SUCCESS",
                "duplicate_field": matched,
                "action": "update",
                "details": {"Modified_Time": stamp, "id": existing},
                "message": "record updated",
                "status": "success",
            }
        for api_name, meta in target.fields.items():
            if meta.get("system_mandatory") and record.get(api_name) in (None, ""):
                return record_error("MANDATORY_NOT_FOUND", "required field not found", api_name=api_name, json_path=f"$.data[{index}].{api_name}")
        for api_name in target.unique_fields():
            if record.get(api_name) not in (None, "") and target.find(api_name, record[api_name]):
                return record_error("DUPLICATE_DATA", "duplicate data", api_name=api_name, json_path=f"$.data[{index}].{api_name}")
        record_id = state.new_id()
        target.records[record_id] = {**values, "Created_Time": stamp, "Modified_Time": stamp}
        state.stats["records_inserted"] += 1
        return {
            "code": "SUCCESS",
            "duplicate_field": None,
            "action": "insert",
            "details": {"Created_Time": stamp, "Modified_Time": stamp, "id": record_id},
            "message": "record added",
            "status": "success",
        }

    @app.post(f"{API}/{{module}}/upsert")
    async def upsert(request: Request, module: str):
        body = await read_json(request)
        records = body.get("data") if isinstance(body, dict) else None
        count = len(records) if isinstance(records, list) else 0
        if refusal := guard(request, f"ZohoCRM.modules.{module.lower()}.ALL", max(1, math.ceil(count / 10))):
            return refusal
        target = module_or_error(module)
        if isinstance(target, JSONResponse):
            return target
        if not count:
            return zoho_error(400, "INVALID_DATA", "invalid data", {"api_name": "data"})
        if count > MAX_UPSERT:
            return zoho_error(400, "LIMIT_EXCEEDED", f"at most {MAX_UPSERT} records per call", {"limit": MAX_UPSERT})
        checks = body.get("duplicate_check_fields") or []
        if not isinstance(checks, list) or not checks or any(name not in target.unique_fields() for name in checks):
            return zoho_error(400, "INVALID_DATA", "duplicate_check_fields must name unique fields of the module", {"api_name": "duplicate_check_fields"})
        results = [upsert_one(target, index, record, checks) for index, record in enumerate(records)]
        if state.roll(chaos.lost_write_rate):
            state.stats["injected_lost_writes"] += 1
            return zoho_error(500, "INTERNAL_ERROR", "Internal Server Error")  # saved, but the caller never hears it
        succeeded = sum(item["status"] == "success" for item in results)
        status = 200 if succeeded == len(results) else 400 if succeeded == 0 else 207
        return answer({"data": results}, status)

    @app.get("/_admin/stats")
    async def stats():
        return {
            "credits_used": state.credits_used,
            "daily_credits": config.daily_credits,
            "records": {name: len(module.records) for name, module in state.modules.items()},
            "stats": dict(state.stats),
        }

    return app


app = create_app()
