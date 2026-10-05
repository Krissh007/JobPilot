"""Token bucket so calls stay under the Gemini free tier's requests-per-minute limit."""

from __future__ import annotations

import threading
import time
from typing import Callable


class TokenBucket:
    def __init__(self, rate_per_minute: float, capacity: float | None = None,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate = rate_per_minute / 60.0  # tokens per second
        self.capacity = capacity if capacity is not None else max(1.0, rate_per_minute / 4)
        self.tokens = self.capacity
        self.clock, self.sleep = clock, sleep
        self.updated = clock()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def acquire(self) -> float:
        """Block until a token is free. Returns the seconds waited."""
        waited = 0.0
        with self._lock:
            self._refill()
            while self.tokens < 1:
                delay = (1 - self.tokens) / self.rate
                self.sleep(delay)
                waited += delay
                self._refill()
            self.tokens -= 1
        return waited
