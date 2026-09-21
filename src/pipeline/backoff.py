"""Retry delays and the adaptive concurrency limiter (design doc 02, section 5.4).

``compute_delay`` is a pure function (jitter comes from an injectable
``random.Random``); ``AdaptiveLimiter`` implements AIMD over asyncio permits
(E-07: concurrency is reduced temporarily after a rate limit).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, List, Optional, Tuple

__all__ = ["AdaptiveLimiter", "BackoffPolicy", "compute_delay"]


def compute_delay(
    attempt: int,
    *,
    base_s: float,
    factor: float = 2.0,
    cap_s: float,
    retry_after_s: Optional[float],
    rng: Optional[random.Random] = None,
) -> float:
    """``min(cap, base * factor**attempt)`` with full jitter, floored by Retry-After.

    ``attempt`` is 0-based (the first retry uses ``attempt=0``).
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    ceiling = min(cap_s, base_s * (factor**attempt))
    jittered = (rng if rng is not None else random).uniform(0.0, ceiling)
    if retry_after_s is not None and retry_after_s > 0:
        return max(jittered, float(retry_after_s))
    return jittered


@dataclass(frozen=True)
class BackoffPolicy:
    """Backoff parameters (defaults per design doc 02, 5.4)."""

    base_s: float = 2.0
    factor: float = 2.0
    cap_s: float = 60.0
    max_retries: int = 5
    rng: Optional[random.Random] = field(default=None, compare=False, repr=False)

    def next_delay(self, attempt: int, retry_after_s: Optional[float] = None) -> float:
        return compute_delay(
            attempt,
            base_s=self.base_s,
            factor=self.factor,
            cap_s=self.cap_s,
            retry_after_s=retry_after_s,
            rng=self.rng,
        )

    def can_retry(self, retry_count: int) -> bool:
        """True while ``retry_count`` (retries already made) is below ``max_retries``."""
        return retry_count < self.max_retries


class AdaptiveLimiter:
    """AIMD permit pool: halve on rate limit, +1 after ``growth_after`` clean successes.

    Usable as ``async with limiter:``. Reducing ``permits`` never interrupts
    in-flight work; new ``acquire`` calls wait until ``in_flight < permits``.
    Not thread-safe: all calls must come from the event-loop thread.
    """

    def __init__(
        self,
        max_permits: int,
        *,
        now: Callable[[], float] = time.monotonic,
        cooldown_s: float = 30.0,
        growth_after: int = 20,
        on_change: Optional[Callable[[str, int], None]] = None,
    ) -> None:
        if max_permits < 1:
            raise ValueError("max_permits must be >= 1")
        self.max_permits = max_permits
        self.permits = max_permits
        self.in_flight = 0
        self.changes: List[Tuple[str, int]] = []
        self._now = now
        self._cooldown_s = cooldown_s
        self._growth_after = growth_after
        self._on_change = on_change
        self._successes = 0
        self._cooldown_until = float("-inf")
        self._waiters: Deque[asyncio.Future[None]] = deque()

    # -- AIMD signals -------------------------------------------------------- #

    def _record(self, event: str) -> None:
        self.changes.append((event, self.permits))
        if self._on_change is not None:
            self._on_change(event, self.permits)

    def on_rate_limited(self) -> None:
        """Halve the permits (min 1) and start the growth cooldown."""
        new_permits = max(1, self.permits // 2)
        self._successes = 0
        self._cooldown_until = self._now() + self._cooldown_s
        if new_permits != self.permits:
            self.permits = new_permits
            self._record("rate_limited")

    def on_success(self) -> None:
        """Count a clean success; grow by one permit after ``growth_after`` in a row."""
        if self._now() < self._cooldown_until:
            self._successes = 0
            return
        self._successes += 1
        if self._successes < self._growth_after:
            return
        self._successes = 0
        if self.permits < self.max_permits:
            self.permits += 1
            self._record("grow")
            self._wake()

    # -- permits ------------------------------------------------------------ #

    def _wake(self) -> None:
        while self._waiters and self.in_flight < self.permits:
            waiter = self._waiters.popleft()
            if not waiter.done():
                self.in_flight += 1
                waiter.set_result(None)

    async def acquire(self) -> None:
        if not self._waiters and self.in_flight < self.permits:
            self.in_flight += 1
            return
        waiter: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await waiter
        except asyncio.CancelledError:
            if waiter.done() and not waiter.cancelled():
                self.in_flight -= 1  # permit was granted while we were being cancelled
                self._wake()
            else:
                try:
                    self._waiters.remove(waiter)
                except ValueError:
                    pass
            raise

    def release(self) -> None:
        if self.in_flight <= 0:
            raise RuntimeError("release() called without a matching acquire()")
        self.in_flight -= 1
        self._wake()

    async def __aenter__(self) -> "AdaptiveLimiter":
        await self.acquire()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self.release()
