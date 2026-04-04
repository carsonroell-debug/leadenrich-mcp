"""Usage tracking and metering for the LeadEnrich MCP server.

Tracks per-API-key usage with monthly rolling window.
Free tier: 50 lookups/month. Paid tiers enforced upstream.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("leadenrich")

USAGE_DIR = Path(os.getenv("LEADENRICH_USAGE_DIR", "/tmp/leadenrich-usage"))
FREE_TIER_LIMIT = int(os.getenv("LEADENRICH_FREE_TIER_LIMIT", "50"))

# Pricing per lookup (in cents) — varies by how many providers are hit
PRICE_SINGLE = 5    # $0.05 — one provider returned data
PRICE_DOUBLE = 10   # $0.10 — two providers hit
PRICE_TRIPLE = 15   # $0.15 — all three providers hit


@dataclass
class UsageRecord:
    api_key: str
    month: str  # YYYY-MM
    lookup_count: int = 0
    cost_cents: int = 0
    tier: str = "free"

    def to_dict(self) -> dict:
        # Import here to avoid circular imports
        try:
            from .billing import TIER_LIMITS
            limit = TIER_LIMITS.get(self.tier, FREE_TIER_LIMIT)
        except ImportError:
            limit = FREE_TIER_LIMIT

        return {
            "api_key_prefix": self.api_key[:8] + "...",
            "month": self.month,
            "lookup_count": self.lookup_count,
            "cost_cents": self.cost_cents,
            "cost_usd": f"${self.cost_cents / 100:.2f}",
            "tier": self.tier,
            "limit": limit,
            "remaining": max(0, limit - self.lookup_count),
        }


def _current_month() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m")


def _usage_path(api_key: str, month: str) -> Path:
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    safe_key = api_key[:16].replace("/", "_").replace("\\", "_")
    return USAGE_DIR / f"{safe_key}_{month}.json"


def get_usage(api_key: str, month: str | None = None) -> UsageRecord:
    """Load usage record for a given API key and month."""
    month = month or _current_month()
    path = _usage_path(api_key, month)

    if path.exists():
        data = json.loads(path.read_text())
        return UsageRecord(
            api_key=api_key,
            month=month,
            lookup_count=data.get("lookup_count", 0),
            cost_cents=data.get("cost_cents", 0),
            tier=data.get("tier", "free"),
        )

    return UsageRecord(api_key=api_key, month=month)


def check_quota(api_key: str) -> tuple[bool, UsageRecord]:
    """Check if the API key has remaining quota. Returns (allowed, record)."""
    record = get_usage(api_key)

    if record.tier != "free":
        return True, record

    return record.lookup_count < FREE_TIER_LIMIT, record


def record_lookup(api_key: str, providers_hit: int) -> UsageRecord:
    """Record a lookup and update usage. Returns updated record."""
    month = _current_month()
    record = get_usage(api_key, month)

    record.lookup_count += 1

    if providers_hit >= 3:
        record.cost_cents += PRICE_TRIPLE
    elif providers_hit >= 2:
        record.cost_cents += PRICE_DOUBLE
    else:
        record.cost_cents += PRICE_SINGLE

    path = _usage_path(api_key, month)
    path.write_text(json.dumps({
        "lookup_count": record.lookup_count,
        "cost_cents": record.cost_cents,
        "tier": record.tier,
        "updated_at": time.time(),
    }))

    return record
