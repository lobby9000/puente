# Chaos benchmark

Run on 2026-11-18 with data seed 7: 10,326 source rows (850 sales reps x 12 months, plus injected defects), 10,119 payable business keys and 177 defective ones.

Failure injection, the same for both contenders: 9% of calls get a transient 5xx and 2% hit a revoked token. 5% of batch writes are answered with a 504 after the write landed, and 5% are answered 3 s late, past the 2 s client timeout. The rate limit is 10 calls per second, and batch request number 30 starts a 30 s outage.

| | Naive script | Puente |
|---|---:|---:|
| Payable rows delivered exactly once | 3,320 of 10,119 (32.8%) | 10,119 of 10,119 (100.0%) |
| Payable rows lost without a record | 5,997 | 0 |
| Payable rows still waiting in review | 0 | 0 |
| Rows paid twice (extra payouts) | 804 (904) | 0 (0) |
| Defective rows sent downstream | 4 | 0 |
| Defective rows parked with the right reason | 0 of 177 (0.0%) | 177 of 177 (100.0%) |
| Defective rows that vanished without a record | 173 | 0 |
| Wall time | 57 s | 52 s |

During Puente's run the mock SaaS refused 16 of 161 calls by design (8 transient errors, 3 lost answers, 4 slow answers, 1 revoked tokens), 16 more during the outage and 7 for the rate limit. Puente retried 36 times, opened the circuit breaker 1 time(s), and its automatic replay found 0 parked lines already in the destination (written before their answer was lost) and delivered 299 more.

Audit of the destination after Puente's run, with failure injection off: clean, every key delivered once or parked with a reason.

The naive script sends batches of 100, tries each request 3 times with a fixed 0.5 s pause, uses a new Idempotency-Key per attempt and validates nothing first. See demo/naive.py.
