"""Token bucket: a burst of `capacity` calls, then `refill_per_s` calls per second."""

from __future__ import annotations

import time
from collections.abc import Callable


class TokenBucket:
    def __init__(self, capacity: int, refill_per_s: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._clock = clock
        self._tokens = float(capacity)
        self._updated = clock()

    def take(self) -> float:
        """Spend one token. Returns 0.0 when the call may go ahead, else the seconds to wait."""
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.refill_per_s)
        self._updated = now
        if self._tokens >= 1:
            self._tokens -= 1
            return 0.0
        return (1 - self._tokens) / self.refill_per_s
