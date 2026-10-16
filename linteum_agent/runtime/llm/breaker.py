"""Provider circuit breaker (§45.2) — same shape as the plugin breaker (§28.2).

Granularity is ``(provider, model)``.  A window of recent outcomes is kept; once
the failure rate crosses the threshold the pair is skipped (the router drops it
from candidates) until the cooldown elapses, at which point it is retried (half-open).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Callable

DEFAULT_WINDOW = 20
DEFAULT_FAILURE_RATE = 0.5
DEFAULT_COOLDOWN = 30.0


class ProviderCircuitBreaker:
    def __init__(
        self,
        *,
        window: int = DEFAULT_WINDOW,
        failure_rate: float = DEFAULT_FAILURE_RATE,
        cooldown: float = DEFAULT_COOLDOWN,
        min_samples: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.window = window
        self.failure_rate = failure_rate
        self.cooldown = cooldown
        self.min_samples = min_samples
        self._clock = clock
        self._outcomes: dict = {}
        self._opened_at: dict = {}

    def record(self, key, ok: bool) -> None:
        outcomes = self._outcomes.setdefault(key, deque(maxlen=self.window))
        outcomes.append(bool(ok))
        if ok:
            self._opened_at.pop(key, None)  # a success closes the breaker (half-open -> closed)
            return
        if len(outcomes) >= self.min_samples and self._rate(outcomes) >= self.failure_rate:
            self._opened_at.setdefault(key, self._clock())

    def _rate(self, outcomes: deque) -> float:
        return outcomes.count(False) / len(outcomes)

    def allow(self, key) -> bool:
        opened = self._opened_at.get(key)
        if opened is None:
            return True
        if self._clock() - opened >= self.cooldown:
            self._opened_at.pop(key, None)  # cooldown elapsed -> half-open
            self._outcomes.pop(key, None)
            return True
        return False

    def state(self, key) -> str:
        return "open" if not self.allow(key) else "closed"
