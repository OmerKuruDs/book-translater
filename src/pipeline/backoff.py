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
from typing import Awaitable, Callable, Deque, List, Optional, Tuple

__all__ = ["AdaptiveLimiter", "BackoffPolicy", "compute_delay"]


def compute_delay(
    attempt: int,
    *,
    base_s: float,
    factor: float = 2.0,
    cap_s: float,
    retry_after_s: Optional[float],
    rng: Optional[random.Random] = None,
    floor_s: float = 0.0,
) -> float:
    """``min(cap, base * factor**attempt)`` with full jitter, floored by Retry-After.

    ``attempt`` is 0-based (the first retry uses ``attempt=0``). ``floor_s`` raises the
    result the same way ``retry_after_s`` does: full jitter draws uniformly from
    ``[0, ceiling]``, so without a floor a run can spend a whole retry budget in seconds -
    which is exactly how 2066 rate-limited units burned six attempts and died. It is
    capped by ``cap_s`` so a floor can never push a wait past the ceiling of the policy.
    """
    if attempt < 0:
        raise ValueError("attempt must be >= 0")
    ceiling = min(cap_s, base_s * (factor**attempt))
    jittered = (rng if rng is not None else random).uniform(0.0, ceiling)
    lower = min(max(floor_s, 0.0), cap_s)
    if retry_after_s is not None and retry_after_s > 0:
        lower = max(lower, float(retry_after_s))
    return max(jittered, lower)


@dataclass(frozen=True)
class BackoffPolicy:
    """Backoff parameters (defaults per design doc 02, 5.4).

    Rate limiting has its own, much larger budget. ``max_retries`` exists to stop a unit
    the provider keeps *refusing*; HTTP 429 is not a refusal, it is "not now". Spending the
    same five attempts on it turned a healthy 364-page job into 2066 permanently FAILED
    units while 40% of the character quota was still unused. When even the large budget
    runs out the job is paused instead of failed - see ``rate_limit_max_retries``.
    """

    base_s: float = 2.0
    factor: float = 2.0
    cap_s: float = 60.0
    max_retries: int = 5
    rate_limit_max_retries: int = 20
    rate_limit_min_delay_s: float = 5.0
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

    def next_rate_limit_delay(
        self, attempt: int, retry_after_s: Optional[float] = None
    ) -> float:
        """:meth:`next_delay` with ``rate_limit_min_delay_s`` as a floor under the jitter."""
        return compute_delay(
            attempt,
            base_s=self.base_s,
            factor=self.factor,
            cap_s=self.cap_s,
            retry_after_s=retry_after_s,
            rng=self.rng,
            floor_s=self.rate_limit_min_delay_s,
        )

    def can_retry(self, retry_count: int) -> bool:
        """True while ``retry_count`` (retries already made) is below ``max_retries``."""
        return retry_count < self.max_retries

    def can_retry_rate_limited(self, rate_limited_count: int) -> bool:
        """True while the *separate* rate-limit budget of this run is not spent.

        ``rate_limited_count`` counts the 429s this unit has already taken in this run; it
        is deliberately not the stored ``retry_count``, which stays reserved for errors the
        unit itself is responsible for.
        """
        return rate_limited_count < self.rate_limit_max_retries


class AdaptiveLimiter:
    """AIMD permit pool: halve on rate limit, +1 after ``growth_after`` clean successes.

    Usable as ``async with limiter:``. Reducing ``permits`` never interrupts
    in-flight work; new ``acquire`` calls wait until ``in_flight < permits``.
    Not thread-safe: all calls must come from the event-loop thread.

    Halving alone stops helping once ``permits`` is 1 - and that is where a throttled run
    spends most of its time. ``cool_off_s`` adds the missing brake: after a 429 *no* new
    request leaves for that long, so the batches already claimed stop walking into the same
    wall. It is pool-wide and complements the per-unit backoff, which only paces the unit
    that was refused.
    """

    def __init__(
        self,
        max_permits: int,
        *,
        now: Callable[[], float] = time.monotonic,
        cooldown_s: float = 30.0,
        growth_after: int = 20,
        on_change: Optional[Callable[[str, int], None]] = None,
        cool_off_s: float = 0.0,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
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
        self._cool_off_s = cool_off_s
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._cool_off_until = float("-inf")
        self._successes = 0
        self._cooldown_until = float("-inf")
        self._waiters: Deque[asyncio.Future[None]] = deque()

    # -- AIMD signals -------------------------------------------------------- #

    def _record(self, event: str) -> None:
        self.changes.append((event, self.permits))
        if self._on_change is not None:
            self._on_change(event, self.permits)

    def on_rate_limited(self) -> None:
        """Halve the permits (min 1), start the growth cooldown and the pool-wide cool-off."""
        new_permits = max(1, self.permits // 2)
        self._successes = 0
        self._cooldown_until = self._now() + self._cooldown_s
        if self._cool_off_s > 0:
            self._cool_off_until = max(self._cool_off_until, self._now() + self._cool_off_s)
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

    async def cool_off(self) -> None:
        """Block until the cool-off started by the last 429 has run out."""
        while True:
            remaining = self._cool_off_until - self._now()
            if remaining <= 0:
                return
            await self._sleep(remaining)

    async def acquire(self) -> None:
        await self.cool_off()
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
