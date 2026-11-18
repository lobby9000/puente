from pathlib import Path

from demo.generate import SOURCE_FILE, generate
from mock_saas.config import ChaosConfig
from puente.reconcile import reconcile
from puente.sources import CsvSource, SourceRow
from puente.state import StateStore
from puente.sync import run_replay, run_sync
from puente.transform import prepare


def load(tmp_path: Path, vendors: int, months: int, seed: int):
    truth = generate(vendors, months, seed, tmp_path / "data")
    return list(CsvSource(tmp_path / "data" / SOURCE_FILE).rows()), truth


def test_prepare_drops_exact_copies_and_parks_conflicts():
    payees = {"V-00001": {"payee_id": "PY-000001", "currency": "MXN"}, "V-00002": {"payee_id": "PY-000002", "currency": "MXN"}}
    base = {"vendor_code": "V-00001", "store_id": "MX-T001", "country": "MX", "currency": "MXN", "period": "2026-10", "financed_sales": "10", "commission_amount": "1080.00"}
    rows = [
        SourceRow(2, base),
        SourceRow(3, dict(base)),
        SourceRow(4, {**base, "vendor_code": "V-00002"}),
        SourceRow(5, {**base, "vendor_code": "V-00002", "commission_amount": "999.00"}),
        SourceRow(6, {**base, "vendor_code": "V-77777"}),
        SourceRow(7, {**base, "vendor_code": "V-00002", "period": "2026-09", "commission_amount": "-5.00"}),
    ]
    prepared = prepare(rows, payees)
    assert prepared.dropped_rows == [3]
    assert [line["external_id"] for _, line in prepared.lines] == ["2026-10:V-00001"]
    assert sorted((r.row.row_number, r.reason) for r in prepared.rejections) == [
        (4, "conflicting_duplicate"),
        (5, "conflicting_duplicate"),
        (6, "unmapped_payee"),
        (7, "invalid_contract"),
    ]


def test_every_row_is_accounted_for_under_chaos(tmp_path, server, connector, breaker):
    rows, truth = load(tmp_path, vendors=60, months=3, seed=5)
    api = server(payee_count=60, error_rate=0.2, ghost_write_rate=0.15, auth_error_rate=0.05, seed=11)
    store = StateStore(tmp_path / "state.db")
    client = connector(api, max_attempts=8)
    run_sync(client, store, rows, source="test", batch_size=10, breaker=breaker)

    stats = api.app.state.mock.stats  # the run really went through each kind of failure
    assert stats["injected_errors"] and stats["injected_ghost_writes"] and stats["injected_auth_errors"]
    assert stats["idempotent_replays"] == stats["injected_ghost_writes"]

    payouts = list(client.iter_payouts())
    audit = reconcile(rows, payouts, store.open_items())
    assert audit.clean and not audit.not_in_source

    paid = {payout["external_id"] for payout in payouts}
    parked = {item["external_id"]: item["reason"] for item in store.open_items()}
    for key, expected in truth["keys"].items():
        if expected == "paid":
            assert key in paid, key
        else:
            assert parked.get(key) == expected.split(":", 1)[1], key


def test_replay_does_not_pay_twice_when_a_failed_write_had_landed(tmp_path, server, connector, breaker):
    rows, _ = load(tmp_path, vendors=20, months=1, seed=9)
    api = server(payee_count=20, ghost_write_rate=1.0, seed=2)
    store = StateStore(tmp_path / "state.db")
    client = connector(api, max_attempts=1)  # the first lost answer spends the whole budget
    summary = run_sync(client, store, rows, source="test", batch_size=50, breaker=breaker, auto_replay=False)
    parked = store.open_items(("retries_exhausted",))
    written = len(api.app.state.mock.payouts)
    assert parked and summary.lines_delivered == 0
    assert written == len(parked)  # every batch that "failed" had in fact been written

    api.app.state.mock.set_chaos(ChaosConfig())  # the destination is healthy again
    replay = run_replay(client, store, batch_size=50, breaker=breaker)
    assert replay.already_in_destination == len(parked)
    assert replay.lines_delivered == 0
    assert len(api.app.state.mock.payouts) == written
    assert not store.open_items(("retries_exhausted",))
