"""The script many teams write first, kept as the baseline the benchmark measures against.

It does what a quick cron job does: maps sales reps to payees, sends batches of
100 and tries each request up to 3 times with a fixed half-second pause. It has
no backoff and ignores Retry-After. It sends a fresh Idempotency-Key on every
attempt, so a retry after a lost answer is a second payment. It validates
nothing before sending and keeps no record of what failed.
"""

from __future__ import annotations

import time
import uuid

import httpx

from puente.sources import SourceRow


def run_naive(
    base_url: str,
    client_id: str,
    client_secret: str,
    rows: list[SourceRow],
    *,
    batch_size: int = 100,
    timeout_s: float = 2.0,
    attempts: int = 3,
    pause_s: float = 0.5,
) -> dict:
    http = httpx.Client(base_url=base_url, timeout=timeout_s)
    token: dict[str, str | None] = {"value": None}

    def send(method: str, path: str, *, params=None, json=None) -> httpx.Response | None:
        for _ in range(attempts):
            try:
                if token["value"] is None:
                    answer = http.post("/oauth/token", data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret})
                    token["value"] = answer.json()["access_token"] if answer.status_code == 200 else None
                if token["value"] is not None:
                    headers = {"Authorization": f"Bearer {token['value']}"}
                    if method == "POST":
                        headers["Idempotency-Key"] = str(uuid.uuid4())  # new key each time: no real idempotency
                    response = http.request(method, path, params=params, json=json, headers=headers)
                    if response.status_code == 401:
                        token["value"] = None
                    elif response.status_code != 429 and response.status_code < 500:
                        return response
            except httpx.HTTPError:
                pass
            time.sleep(pause_s)
        return None

    started = time.monotonic()
    payees: dict[str, str] = {}
    cursor = None
    while True:
        params = {"limit": 100, **({"cursor": cursor} if cursor else {})}
        response = send("GET", "/v1/payees", params=params)
        if response is None or response.status_code != 200:
            break  # a partial payee list: rows of the missing reps get skipped below
        body = response.json()
        payees.update({payee["vendor_code"]: payee["payee_id"] for payee in body["data"]})
        cursor = body.get("next_cursor")
        if not cursor:
            break

    lines = []
    for row in rows:
        payee = payees.get(row.data.get("vendor_code", ""))
        if payee is None:
            continue  # nobody to pay, and nothing records it
        lines.append(
            {
                "external_id": row.external_id,
                "payee_id": payee,
                "period": row.data.get("period", ""),
                "currency": row.data.get("currency", ""),
                "amount": row.data.get("commission_amount", ""),
            }
        )

    batches_ok = batches_failed = 0
    for start in range(0, len(lines), batch_size):
        response = send("POST", "/v1/payout-batches", json={"lines": lines[start : start + batch_size]})
        if response is not None and response.status_code == 201:
            batches_ok += 1
        else:
            batches_failed += 1  # printed to a log nobody reads, then forgotten
    http.close()
    return {"lines_sent": len(lines), "batches_ok": batches_ok, "batches_failed": batches_failed, "duration_s": round(time.monotonic() - started, 2)}
