"""Cost guardrails: rate limiting, concurrency cap and per-session token budget.

All state is in-memory on purpose — the container is ephemeral and single-replica, so
there is nothing to share and nothing to persist. The chain's hard wall-clock timeout
lives in the orchestrator runner (see `app.main`).
"""

from __future__ import annotations

import asyncio
import time
from collections import deque


class GuardrailError(Exception):
    """A request refused by a guardrail. `code` is stable and shown to the UI."""

    def __init__(self, code: str, message: str, status_code: int = 429):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class TokenBudgetExceeded(GuardrailError):
    def __init__(self, used: int, limit: int):
        super().__init__(
            "token_budget_exceeded",
            f"Session token budget exhausted ({used}/{limit} tokens).",
            status_code=200,
        )


class SlidingWindowLimiter:
    """Counts events per key in a rolling window (default one hour)."""

    def __init__(self, limit: int, window_seconds: float = 3600.0):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        if len(self._hits) > 10_000:
            # Don't let one-off IPs accumulate forever.
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] < self.window}
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window:
            hits.popleft()
        return hits

    def check_and_record(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._prune(key, now)
        if self.limit <= 0 or len(hits) >= self.limit:
            return False
        hits.append(now)
        return True

    def retry_after(self, key: str, now: float | None = None) -> int:
        now = time.monotonic() if now is None else now
        hits = self._hits.get(key)
        if not hits:
            return 0
        return max(1, int(self.window - (now - hits[0])))


class TokenBudget:
    """Tracks tokens spent by one session across all LLM calls."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def ensure_available(self, minimum: int) -> None:
        """Raise before a call that could not produce a useful answer anyway."""
        if self.remaining < minimum:
            raise TokenBudgetExceeded(self.used, self.limit)

    def record(self, tokens: int) -> None:
        self.used += tokens


class Admission:
    """Admission control for new chain runs: per-IP and global hourly limits plus a
    concurrency cap. Use as `async with admission.admit(ip): ...`."""

    def __init__(self, per_ip_per_hour: int, global_per_hour: int, max_concurrent: int):
        self.per_ip = SlidingWindowLimiter(per_ip_per_hour)
        self.global_ = SlidingWindowLimiter(global_per_hour)
        self.max_concurrent = max_concurrent
        self.active = 0
        self._lock = asyncio.Lock()

    async def acquire(self, ip: str) -> None:
        async with self._lock:
            if self.active >= self.max_concurrent:
                raise GuardrailError("busy", "The demo is busy right now — please try again in a minute.", 503)
            if not self.per_ip.check_and_record(ip):
                raise GuardrailError(
                    "rate_limited",
                    f"Too many trips planned from your address. Try again in {self.per_ip.retry_after(ip) // 60 + 1} min.",
                )
            if not self.global_.check_and_record("*"):
                raise GuardrailError("global_limit", "The demo reached its hourly limit — please come back later.")
            self.active += 1

    async def release(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)
