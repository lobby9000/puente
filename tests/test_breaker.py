from puente.breaker import CircuitBreaker


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def test_opens_after_the_threshold_probes_and_closes():
    time = FakeTime()
    breaker = CircuitBreaker(failure_threshold=2, cool_down_s=5, max_open_s=60, clock=time.clock, sleep=time.sleep)
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open" and breaker.times_opened == 1
    assert breaker.allow() and breaker.state == "half_open" and time.sleeps == [5]
    breaker.record_failure()  # the probe failed
    assert breaker.state == "open" and breaker.times_opened == 1
    assert breaker.allow()
    breaker.record_success()
    assert breaker.state == "closed"


def test_gives_up_after_staying_open_too_long():
    time = FakeTime()
    breaker = CircuitBreaker(failure_threshold=1, cool_down_s=10, max_open_s=25, clock=time.clock, sleep=time.sleep)
    breaker.record_failure()
    probes = []
    for _ in range(5):
        allowed = breaker.allow()
        probes.append(allowed)
        if allowed:
            breaker.record_failure()
    assert probes == [True, True, True, False, False]
