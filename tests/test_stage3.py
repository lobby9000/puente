"""Stage 3 end to end: reps in Zoho feed the payee mapping, both systems misbehave, nothing is lost."""

from collections import Counter

from demo.generate_raw import GOLD_FILE, generate_raw, read_csv
from demo.zoho_seed import SeedReport, build_crm, check_outcomes, seed_crm
from puente.payees import CONTACT_FIELDS, from_zoho
from puente.reconcile import reconcile
from puente.sources import CsvSource
from puente.state import StateStore
from puente.sync import run_sync
from puente.zoho_setup import ensure_fields


def test_reps_in_zoho_feed_the_payee_mapping(tmp_path, server, connector, breaker, zoho_server, zoho_connector):
    raw = tmp_path / "raw"
    truth = generate_raw(vendors=120, months=3, seed=5, out_dir=raw)
    rows = list(CsvSource(raw / GOLD_FILE).rows())
    crm = build_crm(read_csv(raw / "comp_plan.csv"), truth["keys"], vendors=120, seed=3)

    zoho_api = zoho_server(chaos={"throttle_rate": 0.1, "error_rate": 0.05, "lost_write_rate": 0.2, "token_revoke_rate": 0.05, "seed": 4})
    zoho = zoho_connector(zoho_api, max_attempts=12)
    assert all(check.usable for check in ensure_fields(zoho))
    first = seed_crm(zoho, crm)
    again = seed_crm(zoho, crm)  # upsert by external key: the second load only updates
    assert all(result.ok for result in first.accounts + first.contacts)
    assert set(SeedReport.tally(again.contacts)) == {"update"}
    assert len(zoho_api.app.state.zoho.modules["Contacts"].records) == len(crm.contacts)

    api = server(payee_count=120, error_rate=0.15, ghost_write_rate=0.1, seed=2)
    client = connector(api, max_attempts=8)
    store = StateStore(tmp_path / "state.db")
    summary = run_sync(
        client,
        store,
        rows,
        source="test",
        batch_size=20,
        breaker=breaker,
        payees=lambda: from_zoho(zoho.iter_records("Contacts", CONTACT_FIELDS, per_page=50), client.list_payees()),
    )
    assert summary.payee_source == "zoho"
    assert summary.payee_stats["blocked:payee_mismatch"] == 1

    audit = reconcile(rows, client.iter_payouts(), store.open_items())
    assert audit.clean and not audit.not_in_source
    matched, wrong = check_outcomes(crm.expected, store.open_items())
    assert wrong == []
    assert matched["review:payout_on_hold"] == 2 * 3 and matched["review:payee_mismatch"] == 3
    # The rep whose account was copied is still paid: only the copy waits for a person.
    victim = next(iter(crm.copied_from.values()))
    paid = Counter(payout["external_id"].split(":", 1)[1] for payout in client.iter_payouts())
    assert paid[victim] == 3
