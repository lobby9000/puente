"""A run: read rows, prepare lines, deliver them in batches and account for every row.

Every row ends in exactly one place: delivered, parked in the review queue with a
reason, or dropped as an exact copy of a row already kept (and logged). The
review queue holds two kinds of reasons. Transient ones (the destination was
down, retries ran out) are replayed by a machine. Data problems wait for a person.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import httpx

from .breaker import CircuitBreaker
from .client import SaaSClient
from .errors import AuthError, RetryExhausted
from .payees import PayeeDirectory, from_destination
from .sources import SourceRow
from .state import REPLAYABLE, StateStore
from .transform import prepare

log = logging.getLogger("puente.sync")

MAX_SPLITS_PER_BATCH = 10

Line = tuple[SourceRow, dict]


def idempotency_key(lines: list[dict]) -> str:
    """Same lines, same key. A batch sent again, by a retry or a re-run, cannot land twice."""
    canonical = json.dumps(lines, sort_keys=True, separators=(",", ":"))
    return "pb-" + hashlib.sha256(canonical.encode()).hexdigest()[:40]


def new_run_id(kind: str) -> str:
    return f"{kind}-{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"


def chunks(items: list, size: int) -> list[list]:
    return [items[start : start + size] for start in range(0, len(items), size)]


@dataclass
class RunSummary:
    run_id: str
    kind: str
    source: str
    status: str = "running"
    rows_in: int = 0
    lines_ready: int = 0
    lines_delivered: int = 0
    already_in_destination: int = 0
    exact_duplicates_dropped: int = 0
    review_added: dict[str, int] = field(default_factory=dict)
    review_resolved: int = 0
    batches_delivered: int = 0
    batches_split: int = 0
    idempotent_replays: int = 0
    breaker_opened: int = 0
    retries: int = 0
    token_fetches: int = 0
    payee_source: str = "destination"
    payee_stats: dict[str, int] = field(default_factory=dict)
    duration_s: float = 0.0
    replay: RunSummary | None = None

    def count_review(self, reason: str, n: int = 1) -> None:
        self.review_added[reason] = self.review_added.get(reason, 0) + n

    def as_dict(self) -> dict:
        data = {key: value for key, value in vars(self).items() if key != "replay"}
        data["replay_run_id"] = self.replay.run_id if self.replay else None
        return data


@dataclass
class BatchOutcome:
    delivered: list[Line] = field(default_factory=list)
    rejected: list[tuple[SourceRow, dict, str]] = field(default_factory=list)
    unresolved: list[tuple[SourceRow, dict, str]] = field(default_factory=list)
    replayed: bool = False
    splits: int = 0


def flagged_lines(response: httpx.Response, size: int) -> dict[int, str]:
    """Line index to reason, from a 422 that names its invalid lines. Empty when it names none."""
    if response.status_code != 422:
        return {}
    try:
        body = response.json()
    except ValueError:
        return {}
    items = body.get("invalid_lines") if isinstance(body, dict) else None
    flagged = {}
    for item in items or []:
        index = item.get("index") if isinstance(item, dict) else None
        if isinstance(index, int) and 0 <= index < size:
            flagged[index] = "; ".join(item.get("errors") or ["rejected"])
    return flagged


def deliver(client: SaaSClient, batch: list[Line]) -> BatchOutcome:
    """Send one batch until every line is delivered, rejected with a reason, or unresolved."""
    outcome = BatchOutcome()
    pending = list(batch)
    while pending:
        lines = [line for _, line in pending]
        try:
            response = client.create_payout_batch(lines, idempotency_key(lines))
        except RetryExhausted as exc:
            outcome.unresolved.extend((row, line, str(exc)) for row, line in pending)
            return outcome
        if response.status_code in (200, 201):
            outcome.delivered.extend(pending)
            outcome.replayed = outcome.replayed or response.headers.get("Idempotent-Replayed") == "true"
            return outcome
        flagged = flagged_lines(response, len(pending))
        if not flagged or outcome.splits >= MAX_SPLITS_PER_BATCH:
            detail = f"HTTP {response.status_code}: {response.text[:300]}"
            outcome.rejected.extend((row, line, detail) for row, line in pending)
            return outcome
        # The destination named the bad lines and wrote nothing. Park those and send
        # the rest as a new, smaller batch (new content, so a new idempotency key).
        outcome.splits += 1
        keep = []
        for index, (row, line) in enumerate(pending):
            if index in flagged:
                outcome.rejected.append((row, line, flagged[index]))
            else:
                keep.append((row, line))
        pending = keep
    return outcome


def park(store: StateStore, summary: RunSummary, items: list[tuple[SourceRow, dict | None, str]], reason: str) -> None:
    store.add_reviews(summary.run_id, [(row, reason, detail, line) for row, line, detail in items])
    if items:
        summary.count_review(reason, len(items))


def record(store: StateStore, summary: RunSummary, outcome: BatchOutcome) -> None:
    if outcome.delivered:
        summary.lines_delivered += len(outcome.delivered)
        summary.batches_delivered += 1
    summary.batches_split += outcome.splits
    summary.idempotent_replays += int(outcome.replayed)
    park(store, summary, outcome.rejected, "rejected_by_destination")
    park(store, summary, outcome.unresolved, "retries_exhausted")


def remaining(batches: list[list[Line]], position: int) -> list[Line]:
    return [item for later in batches[position:] for item in later]


def send_all(client: SaaSClient, store: StateStore, summary: RunSummary, batches: list[list[Line]], breaker: CircuitBreaker) -> None:
    for position, batch in enumerate(batches):
        if not breaker.allow():
            rest = remaining(batches, position)
            detail = f"destination kept failing for {breaker.max_open_s:g} s"
            park(store, summary, [(row, line, detail) for row, line in rest], "circuit_open")
            log.error("circuit_gave_up", extra={"run_id": summary.run_id, "lines_parked": len(rest)})
            return
        try:
            outcome = deliver(client, batch)
        except AuthError as exc:
            park(store, summary, [(row, line, str(exc)) for row, line in remaining(batches, position)], "run_aborted")
            raise
        record(store, summary, outcome)
        if outcome.unresolved:
            breaker.record_failure()
            log.warning("batch_unresolved", extra={"run_id": summary.run_id, "lines": len(outcome.unresolved), "breaker": breaker.state})
        else:
            breaker.record_success()


def _close(store: StateStore, summary: RunSummary, client: SaaSClient, before, started: float, breaker: CircuitBreaker) -> None:
    summary.breaker_opened = breaker.times_opened
    summary.retries = client.metrics["retries"] - before["retries"]
    summary.token_fetches = client.metrics["token_fetches"] - before["token_fetches"]
    summary.duration_s = round(time.monotonic() - started, 2)
    store.finish_run(summary.run_id, summary.status, summary.as_dict())
    log.info(
        "run_finished",
        extra={"run_id": summary.run_id, "kind": summary.kind, "status": summary.status, "delivered": summary.lines_delivered, "review_added": summary.review_added},
    )


def run_sync(
    client: SaaSClient,
    store: StateStore,
    rows: Iterable[SourceRow],
    *,
    source: str,
    batch_size: int = 100,
    breaker: CircuitBreaker | None = None,
    auto_replay: bool = True,
    payees: Callable[[], PayeeDirectory] | None = None,
) -> RunSummary:
    """payees builds the rep-to-payee mapping. By default it comes from the destination."""
    breaker = breaker or CircuitBreaker()
    summary = RunSummary(run_id=new_run_id("sync"), kind="sync", source=source)
    started, before = time.monotonic(), client.metrics.copy()
    store.start_run(summary.run_id, "sync", source)
    log.info("run_started", extra={"run_id": summary.run_id, "source": source})
    try:
        rows = list(rows)
        summary.rows_in = len(rows)
        directory = payees() if payees else from_destination(client.list_payees())
        summary.payee_source = directory.source
        summary.payee_stats = dict(sorted(directory.stats.items()))
        prepared = prepare(rows, directory)
        summary.exact_duplicates_dropped = len(prepared.dropped_rows)
        if prepared.dropped_rows:
            log.info("exact_duplicates_dropped", extra={"run_id": summary.run_id, "source_rows": prepared.dropped_rows})
        store.add_reviews(summary.run_id, [(r.row, r.reason, r.detail, None) for r in prepared.rejections])
        for rejection in prepared.rejections:
            summary.count_review(rejection.reason)
        summary.lines_ready = len(prepared.lines)
        send_all(client, store, summary, chunks(prepared.lines, batch_size), breaker)
        summary.status = "completed"
    except Exception:
        summary.status = "failed"
        log.exception("run_failed", extra={"run_id": summary.run_id})
        raise
    finally:
        _close(store, summary, client, before, started, breaker)

    if auto_replay and breaker.state == "closed" and store.open_items(REPLAYABLE):
        summary.replay = run_replay(client, store, batch_size=batch_size, breaker=breaker)
        store.finish_run(summary.run_id, summary.status, summary.as_dict())
    return summary


def run_replay(client: SaaSClient, store: StateStore, *, batch_size: int = 100, breaker: CircuitBreaker | None = None) -> RunSummary:
    """Retry parked lines whose reason is transient, after asking the destination what it already has."""
    breaker = breaker or CircuitBreaker()
    summary = RunSummary(run_id=new_run_id("replay"), kind="replay", source="review_queue")
    started, before = time.monotonic(), client.metrics.copy()
    store.start_run(summary.run_id, "replay", "review_queue")
    try:
        items = [item for item in store.open_items(REPLAYABLE) if item["payload"].get("line")]
        summary.rows_in = len(items)
        # A write that looked failed (a timeout, a lost answer, a crash) may have landed.
        # Sending it again would pay twice, so reconcile first.
        present = client.find_payouts(item["external_id"] for item in items) if items else {}
        queued: list[tuple[dict, SourceRow, dict]] = []
        for item in items:
            if item["external_id"] in present:
                store.resolve(item["id"], f"already in destination, checked by {summary.run_id}")
                summary.already_in_destination += 1
                summary.review_resolved += 1
            else:
                payload = item["payload"]
                queued.append((item, SourceRow(payload["row_number"], payload["row"]), payload["line"]))
        summary.lines_ready = len(queued)
        for chunk in chunks(queued, batch_size):
            if not breaker.allow():
                break  # what is left stays open for the next replay
            owner = {id(row): item for item, row, _ in chunk}
            outcome = deliver(client, [(row, line) for _, row, line in chunk])
            for row, _ in outcome.delivered:
                store.resolve(owner[id(row)]["id"], f"delivered by {summary.run_id}")
            summary.lines_delivered += len(outcome.delivered)
            summary.review_resolved += len(outcome.delivered)
            summary.idempotent_replays += int(outcome.replayed)
            for row, _, detail in outcome.rejected:
                store.reclassify(owner[id(row)]["id"], "rejected_by_destination", detail)
                summary.count_review("rejected_by_destination")
            for row, _, detail in outcome.unresolved:
                store.retry_later(owner[id(row)]["id"], detail)
            if outcome.unresolved:
                breaker.record_failure()
            else:
                breaker.record_success()
        summary.status = "completed"
    except Exception:
        summary.status = "failed"
        log.exception("run_failed", extra={"run_id": summary.run_id})
        raise
    finally:
        _close(store, summary, client, before, started, breaker)
    return summary
