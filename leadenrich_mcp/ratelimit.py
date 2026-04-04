"""In-memory sliding window rate limiter for the LeadEnrich MCP server.

Per-API-key, tier-based rate limiting with auto-cleanup of stale entries.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

log = logging.getLogger("leadenrich")

# Requests per minute by tier
TIER_LIMITS: dict[str, int] = {
    "free": 10,
    "starter": 30,
    "pro": 60,
    "scale": 120,
}

WINDOW_SECONDS = 60.0
CLEANUP_INTERVAL = 300.0  # purge stale entries every 5 minutes


@dataclass
class RateLimitResult:
    allowed: bool
    remaining: int
    reset_seconds: float


class RateLimiter:
    """Sliding window rate limiter keyed by API key + tier."""

    def __init__(self) -> None:
        # api_key -> sorted list of request timestamps
        self._windows: dict[str, list[float]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._last_cleanup = time.monotonic()

    async def check(self, api_key: str, tier: str = "free") -> RateLimitResult:
        """Check (and record) a request against the rate limit.

        Returns a RateLimitResult with allowed, remaining, and reset_seconds.
        """
        limit = TIER_LIMITS.get(tier, TIER_LIMITS["free"])
        now = time.monotonic()
        cutoff = now - WINDOW_SECONDS

        async with self._lock:
            # Prune expired timestamps for this key
            timestamps = self._windows[api_key]
            self._windows[api_key] = [t for t in timestamps if t > cutoff]
            timestamps = self._windows[api_key]

            # Periodic cleanup of all keys to prevent memory leaks
            if now - self._last_cleanup > CLEANUP_INTERVAL:
                self._cleanup(now)

            count = len(timestamps)

            if count >= limit:
                # Denied — calculate when the oldest request in the window expires
                reset = timestamps[0] - cutoff
                return RateLimitResult(
                    allowed=False,
                    remaining=0,
                    reset_seconds=round(max(reset, 0.0), 2),
                )

            # Allowed — record this request
            timestamps.append(now)
            remaining = limit - len(timestamps)
            # Reset = time until the oldest timestamp falls out of the window
            reset = (timestamps[0] - cutoff) if timestamps else WINDOW_SECONDS
            return RateLimitResult(
                allowed=True,
                remaining=remaining,
                reset_seconds=round(max(reset, 0.0), 2),
            )

    def _cleanup(self, now: float) -> None:
        """Remove keys with no recent requests. Called under lock."""
        cutoff = now - WINDOW_SECONDS
        stale_keys = [
            key for key, ts in self._windows.items()
            if not ts or ts[-1] <= cutoff
        ]
        for key in stale_keys:
            del self._windows[key]
        if stale_keys:
            log.debug("rate-limiter cleanup: removed %d stale keys", len(stale_keys))
        self._last_cleanup = now


# Module-level singleton — import and use directly
rate_limiter = RateLimiter()
