"""Circuit breaker: stop hammering a destination that is down, probe it, resume."""

from __future__ import annotations

import time
from collections.abc import Callable


class CircuitBreaker:
    """Closed: calls flow. Open: wait out a cool-down. Half-open: one probe decides.

    After failure_threshold failed batches in a row the circuit opens. Each probe
    that fails sends it back to open. If it stays open longer than max_open_s, the
    run gives up and parks what is left for a later replay.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        cool_down_s: float = 5.0,
        max_open_s: float = 120.0,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.cool_down_s = cool_down_s
        self.max_open_s = max_open_s
        self._clock = clock
        self._sleep = sleep
        self.state = "closed"
        self.consecutive_failures = 0
        self.open_since: float | None = None
        self.times_opened = 0

    def allow(self) -> bool:
        if self.state != "open":
            return True
        if self._clock() - self.open_since >= self.max_open_s:
            return False
        self._sleep(self.cool_down_s)
        self.state = "half_open"
        return True

    def record_success(self) -> None:
        self.state = "closed"
        self.consecutive_failures = 0
        self.open_since = None

    def record_failure(self) -> None:
        self.consecutive_failures += 1
        if self.state == "half_open" or self.consecutive_failures >= self.failure_threshold:
            if self.open_since is None:
                self.open_since = self._clock()
                self.times_opened += 1
            self.state = "open"
