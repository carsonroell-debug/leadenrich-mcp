"""
LeadEnrich MCP Server — Waterfall lead enrichment via Apollo, Clearbit, and Hunter.

Cascades through multiple providers to maximize data coverage. Each lookup tries
all configured providers (concurrently when possible) and merges into one profile.

Run locally:
    fastmcp run main.py --transport streamable-http --port 8300

Environment variables:
    APOLLO_API_KEY      — Apollo.io API key
    CLEARBIT_API_KEY    — Clearbit API key
    HUNTER_API_KEY      — Hunter.io API key
    LEADENRICH_API_KEY  — API key for authenticating MCP clients (optional, for metering)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

from fastmcp import FastMCP

from .providers import (
    waterfall_enrich,
    enrich_hunter,
    cache_stats as provider_cache_stats,
    clear_cache as provider_clear_cache,
    get_client,
)
from .usage import check_quota, record_lookup, get_usage, FREE_TIER_LIMIT
from .billing import (
    handle_plans_route,
    handle_checkout_route,
    handle_webhook_route,
    check_quota_with_tier,
    PLANS,
    TIER_LIMITS,
)
from .ratelimit import rate_limiter

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

APOLLO_KEY = os.getenv("APOLLO_API_KEY", "")
CLEARBIT_KEY = os.getenv("CLEARBIT_API_KEY", "")
HUNTER_KEY = os.getenv("HUNTER_API_KEY", "")
SERVER_API_KEY = os.getenv("LEADENRICH_API_KEY", "")

log = logging.getLogger("leadenrich")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    stream=sys.stderr,
)

# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "LeadEnrich",
    instructions=(
        "LeadEnrich is a waterfall lead enrichment server. Give it an email, "
        "domain, or name and it cascades through Apollo, Clearbit, and Hunter "
        "to return the most complete lead profile possible. Use enrich_lead for "
        "single lookups, enrich_batch for multiple leads, find_email to discover "
        "an email from name + domain, enrich_company for company-only data, "
        "and check_usage to monitor your quota."
    ),
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _configured_providers() -> list[str]:
    providers = []
    if APOLLO_KEY:
        providers.append("apollo")
    if CLEARBIT_KEY:
        providers.append("clearbit")
    if HUNTER_KEY:
        providers.append("hunter")
    return providers


def _cost_label(providers_hit: int) -> str:
    if providers_hit >= 3:
        return "$0.15"
    if providers_hit >= 2:
        return "$0.10"
    if providers_hit >= 1:
        return "$0.05"
    return "$0.00"


def _check_and_record(client_key: str, providers_hit: int) -> None:
    """Record a lookup if providers were hit."""
    if providers_hit > 0:
        record_lookup(client_key, providers_hit)


async def _guard(client_key: str) -> dict | None:
    """Check quota + rate limit. Returns error dict if blocked, None if OK."""
    allowed, usage = check_quota_with_tier(client_key)
    if not allowed:
        limit = TIER_LIMITS.get(usage.tier, FREE_TIER_LIMIT)
        return {
            "error": f"Quota exceeded for {usage.tier} tier",
            "usage": usage.to_dict(),
            "message": f"{usage.tier.title()} tier limit is {limit} lookups/month. Upgrade at /api/plans.",
        }

    rl = await rate_limiter.check(client_key, usage.tier)
    if not rl.allowed:
        return {
            "error": "Rate limit exceeded",
            "tier": usage.tier,
            "retry_after_seconds": rl.reset_seconds,
            "message": f"Too many requests. Try again in {rl.reset_seconds}s.",
        }

    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def enrich_lead(
    email: str | None = None,
    domain: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    providers: list[str] | None = None,
    api_key: str | None = None,
) -> dict:
    """Enrich a single lead by cascading through Apollo, Clearbit, and Hunter.

    Provide at least one of: email, domain, or first_name + last_name + domain.
    When email is provided, all providers run concurrently for speed.
    When only domain/name is provided, Apollo runs first to discover the email,
    then Clearbit + Hunter run concurrently.

    Args:
        email: Contact email address (best identifier).
        domain: Company domain (e.g. "stripe.com").
        first_name: Contact's first name (combine with last_name + domain).
        last_name: Contact's last name.
        providers: Optional list to limit which providers to use. Default: all configured.
        api_key: Your LeadEnrich API key for usage tracking.

    Returns:
        Merged lead profile with field attribution showing which provider contributed each field.
    """
    if not email and not domain and not (first_name and last_name):
        return {"error": "Provide at least email, domain, or first_name + last_name"}

    client_key = api_key or SERVER_API_KEY or "anonymous"
    if err := await _guard(client_key):
        return err

    merged, providers_hit = await waterfall_enrich(
        apollo_key=APOLLO_KEY, clearbit_key=CLEARBIT_KEY, hunter_key=HUNTER_KEY,
        email=email, domain=domain,
        first_name=first_name, last_name=last_name,
        providers=providers,
    )

    _check_and_record(client_key, providers_hit)
    merged["timestamp"] = _ts()
    merged["lookup_cost"] = _cost_label(providers_hit)
    return merged


@mcp.tool()
async def find_email(
    first_name: str,
    last_name: str,
    domain: str,
    api_key: str | None = None,
) -> dict:
    """Find someone's email address given their name and company domain.

    Uses Hunter email-finder first (purpose-built for this), then falls back
    to Apollo people-match if Hunter doesn't find it.

    Args:
        first_name: Contact's first name.
        last_name: Contact's last name.
        domain: Company domain (e.g. "stripe.com").
        api_key: Your LeadEnrich API key for usage tracking.

    Returns:
        Found email with confidence score and verification status.
    """
    client_key = api_key or SERVER_API_KEY or "anonymous"
    if err := await _guard(client_key):
        return err

    providers_hit = 0

    # Try Hunter first — it's built for email finding
    if HUNTER_KEY:
        result = await enrich_hunter(
            HUNTER_KEY, domain=domain,
            first_name=first_name, last_name=last_name,
        )
        if result and result.email:
            providers_hit = 1
            _check_and_record(client_key, providers_hit)
            return {
                "email": result.email,
                "first_name": first_name,
                "last_name": last_name,
                "domain": domain,
                "confidence": round(result.confidence, 2),
                "provider": "hunter",
                "timestamp": _ts(),
                "lookup_cost": _cost_label(providers_hit),
            }

    # Fall back to full waterfall
    merged, providers_hit = await waterfall_enrich(
        apollo_key=APOLLO_KEY, clearbit_key=CLEARBIT_KEY, hunter_key=HUNTER_KEY,
        domain=domain, first_name=first_name, last_name=last_name,
    )

    _check_and_record(client_key, providers_hit)

    if merged.get("email"):
        return {
            "email": merged["email"],
            "first_name": first_name,
            "last_name": last_name,
            "domain": domain,
            "confidence": merged.get("confidence", 0),
            "provider": merged.get("providers_used", ["unknown"])[0],
            "timestamp": _ts(),
            "lookup_cost": _cost_label(providers_hit),
        }

    return {
        "error": "Email not found",
        "first_name": first_name,
        "last_name": last_name,
        "domain": domain,
        "providers_tried": merged.get("providers_used", []),
        "timestamp": _ts(),
    }


@mcp.tool()
async def enrich_company(
    domain: str,
    api_key: str | None = None,
) -> dict:
    """Enrich a company by domain. Returns firmographic data without person-level details.

    Best for account-level research: industry, size, revenue, description, etc.

    Args:
        domain: Company domain (e.g. "stripe.com").
        api_key: Your LeadEnrich API key for usage tracking.

    Returns:
        Company profile with firmographic data.
    """
    client_key = api_key or SERVER_API_KEY or "anonymous"
    if err := await _guard(client_key):
        return err

    merged, providers_hit = await waterfall_enrich(
        apollo_key=APOLLO_KEY, clearbit_key=CLEARBIT_KEY, hunter_key=HUNTER_KEY,
        domain=domain, providers=["clearbit", "apollo", "hunter"],
    )

    _check_and_record(client_key, providers_hit)

    # Filter to company-level fields only
    company_fields = {
        "company", "company_domain", "industry", "company_size",
        "company_revenue", "company_founded", "company_description",
        "location", "phone", "twitter_url",
        "providers_used", "fields_by_provider", "total_fields", "confidence",
    }
    result = {k: v for k, v in merged.items() if k in company_fields or k == "error"}
    result["timestamp"] = _ts()
    result["lookup_cost"] = _cost_label(providers_hit)
    return result


@mcp.tool()
async def enrich_batch(
    leads: list[dict],
    providers: list[str] | None = None,
    api_key: str | None = None,
) -> dict:
    """Enrich multiple leads concurrently. Each lead cascades through all providers.

    Args:
        leads: List of lead objects, each with optional keys: email, domain, first_name, last_name.
        providers: Optional list to limit which providers to use.
        api_key: Your LeadEnrich API key for usage tracking.

    Returns:
        List of enriched lead profiles with per-lead attribution and batch summary.
    """
    if not leads:
        return {"error": "No leads provided"}
    if len(leads) > 25:
        return {"error": "Batch limit is 25 leads per request"}

    client_key = api_key or SERVER_API_KEY or "anonymous"
    if err := await _guard(client_key):
        return err

    # Run all leads concurrently (capped at 5 concurrent to avoid rate limits)
    sem = asyncio.Semaphore(5)

    async def _enrich_one(lead: dict) -> dict:
        async with sem:
            ok, _ = check_quota_with_tier(client_key)
            if not ok:
                return {"input": lead, "error": "Quota exhausted mid-batch"}

            merged, hits = await waterfall_enrich(
                apollo_key=APOLLO_KEY, clearbit_key=CLEARBIT_KEY, hunter_key=HUNTER_KEY,
                email=lead.get("email"), domain=lead.get("domain"),
                first_name=lead.get("first_name"), last_name=lead.get("last_name"),
                providers=providers,
            )
            _check_and_record(client_key, hits)
            merged["input"] = lead
            merged["lookup_cost"] = _cost_label(hits)
            return merged

    results = await asyncio.gather(*[_enrich_one(lead) for lead in leads])

    return {
        "results": list(results),
        "total": len(results),
        "enriched": sum(1 for r in results if "error" not in r),
        "timestamp": _ts(),
    }


@mcp.tool()
async def check_usage(
    api_key: str | None = None,
) -> dict:
    """Check your current usage and remaining quota.

    Args:
        api_key: Your LeadEnrich API key. Uses server default if not provided.

    Returns:
        Usage stats: lookup count, cost, tier, and remaining quota.
    """
    client_key = api_key or SERVER_API_KEY or "anonymous"
    usage = get_usage(client_key)
    data = usage.to_dict()
    data["cache"] = provider_cache_stats()
    return data


@mcp.tool()
async def health_check() -> dict:
    """Check server health and which enrichment providers are configured.

    Returns server status, configured providers, cache stats, and connectivity info.
    """
    return {
        "status": "ok",
        "server": "LeadEnrich MCP",
        "version": "0.2.0",
        "configured_providers": _configured_providers(),
        "provider_status": {
            "apollo": "configured" if APOLLO_KEY else "not configured",
            "clearbit": "configured" if CLEARBIT_KEY else "not configured",
            "hunter": "configured" if HUNTER_KEY else "not configured",
        },
        "free_tier_limit": FREE_TIER_LIMIT,
        "cache": provider_cache_stats(),
        "api_key_required": bool(SERVER_API_KEY),
        "timestamp": _ts(),
    }


# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.resource("leadenrich://example-result")
async def example_result() -> str:
    """Example enrichment result showing the merged output format."""
    example = {
        "email": "jane@stripe.com",
        "first_name": "Jane",
        "last_name": "Smith",
        "full_name": "Jane Smith",
        "title": "VP of Engineering",
        "company": "Stripe",
        "company_domain": "stripe.com",
        "linkedin_url": "https://linkedin.com/in/janesmith",
        "phone": "+1-555-0100",
        "location": "San Francisco, CA, US",
        "industry": "Financial Services",
        "company_size": "8000",
        "company_revenue": "$1B+",
        "company_founded": "2010",
        "company_description": "Stripe is a technology company that builds economic infrastructure for the internet.",
        "twitter_url": "https://twitter.com/stripe",
        "confidence": 0.9,
        "providers_used": ["apollo", "clearbit", "hunter"],
        "fields_by_provider": {
            "apollo": ["email", "first_name", "last_name", "full_name", "title", "company", "linkedin_url"],
            "clearbit": ["phone", "location", "industry", "company_size", "company_revenue", "company_founded", "company_description", "twitter_url"],
            "hunter": ["company_domain"],
        },
        "total_fields": 16,
        "timestamp": "2026-04-04T12:00:00+00:00",
        "lookup_cost": "$0.15",
    }
    return json.dumps(example, indent=2)


# ---------------------------------------------------------------------------
# Custom routes
# ---------------------------------------------------------------------------

from starlette.requests import Request
from starlette.responses import JSONResponse, HTMLResponse


@mcp.custom_route("/", methods=["GET"])
async def landing(request: Request) -> HTMLResponse:
    providers = _configured_providers()
    cache = provider_cache_stats()
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>LeadEnrich — Waterfall Lead Enrichment for AI Agents</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0a; color: #e0e0e0; min-height: 100vh; display: flex; flex-direction: column; align-items: center; padding: 3rem 1.5rem; }}
  .container {{ max-width: 720px; width: 100%; }}
  h1 {{ font-size: 2.5rem; font-weight: 700; color: #fff; margin-bottom: 0.5rem; }}
  h1 span {{ color: #6366f1; }}
  .tagline {{ font-size: 1.15rem; color: #888; margin-bottom: 2.5rem; line-height: 1.6; }}
  .status {{ display: inline-flex; align-items: center; gap: 0.5rem; background: #111; border: 1px solid #222; border-radius: 999px; padding: 0.4rem 1rem; font-size: 0.85rem; margin-bottom: 2rem; }}
  .dot {{ width: 8px; height: 8px; background: #6366f1; border-radius: 50%; animation: pulse 2s infinite; }}
  @keyframes pulse {{ 0%, 100% {{ opacity: 1; }} 50% {{ opacity: 0.4; }} }}
  .section {{ background: #111; border: 1px solid #1a1a1a; border-radius: 12px; padding: 1.5rem; margin-bottom: 1.25rem; }}
  .section h2 {{ font-size: 1rem; color: #6366f1; margin-bottom: 0.75rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }}
  .tools {{ display: grid; gap: 0.5rem; }}
  .tool {{ display: flex; justify-content: space-between; align-items: baseline; padding: 0.5rem 0; border-bottom: 1px solid #1a1a1a; }}
  .tool:last-child {{ border-bottom: none; }}
  .tool-name {{ font-family: 'SF Mono', 'Fira Code', monospace; color: #6366f1; font-size: 0.9rem; }}
  .tool-desc {{ color: #666; font-size: 0.85rem; text-align: right; }}
  pre {{ background: #0d0d0d; border: 1px solid #1a1a1a; border-radius: 8px; padding: 1rem; overflow-x: auto; font-size: 0.85rem; color: #ccc; line-height: 1.5; }}
  .pricing {{ display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 0.75rem; }}
  .price-card {{ background: #0d0d0d; border: 1px solid #1a1a1a; border-radius: 8px; padding: 1rem; text-align: center; }}
  .price-card .amount {{ font-size: 1.5rem; font-weight: 700; color: #6366f1; }}
  .price-card .label {{ font-size: 0.75rem; color: #666; margin-top: 0.25rem; }}
  .providers {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .provider {{ display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.4rem 0.8rem; background: #0d0d0d; border: 1px solid #1a1a1a; border-radius: 6px; font-size: 0.85rem; }}
  .provider .dot-sm {{ width: 6px; height: 6px; border-radius: 50%; }}
  .dot-on {{ background: #22c55e; }}
  .dot-off {{ background: #555; }}
  .stats {{ display: flex; gap: 2rem; margin-top: 0.75rem; }}
  .stat {{ text-align: center; }}
  .stat-val {{ font-size: 1.25rem; font-weight: 700; color: #fff; }}
  .stat-label {{ font-size: 0.7rem; color: #666; text-transform: uppercase; letter-spacing: 0.05em; }}
  a {{ color: #6366f1; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .footer {{ margin-top: 2rem; color: #444; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<div class="container">
  <div class="status"><span class="dot"></span> Operational</div>
  <h1>Lead<span>Enrich</span></h1>
  <p class="tagline">Waterfall lead enrichment for AI agents. Cascades Apollo, Clearbit, and Hunter concurrently to build the most complete lead profile in a single call.</p>

  <div class="section">
    <h2>Connect</h2>
    <pre>{{
  "mcpServers": {{
    "leadenrich": {{
      "url": "http://localhost:8300/mcp"
    }}
  }}
}}</pre>
  </div>

  <div class="section">
    <h2>Providers</h2>
    <div class="providers">
      <div class="provider"><span class="dot-sm {"dot-on" if "apollo" in providers else "dot-off"}"></span> Apollo</div>
      <div class="provider"><span class="dot-sm {"dot-on" if "clearbit" in providers else "dot-off"}"></span> Clearbit</div>
      <div class="provider"><span class="dot-sm {"dot-on" if "hunter" in providers else "dot-off"}"></span> Hunter</div>
    </div>
    <div class="stats">
      <div class="stat"><div class="stat-val">{len(providers)}/3</div><div class="stat-label">Active</div></div>
      <div class="stat"><div class="stat-val">{cache['valid_entries']}</div><div class="stat-label">Cached</div></div>
    </div>
  </div>

  <div class="section">
    <h2>Tools</h2>
    <div class="tools">
      <div class="tool"><span class="tool-name">enrich_lead</span><span class="tool-desc">Full waterfall enrichment</span></div>
      <div class="tool"><span class="tool-name">find_email</span><span class="tool-desc">Name + domain to email</span></div>
      <div class="tool"><span class="tool-name">enrich_company</span><span class="tool-desc">Company firmographics by domain</span></div>
      <div class="tool"><span class="tool-name">enrich_batch</span><span class="tool-desc">Concurrent batch (up to 25)</span></div>
      <div class="tool"><span class="tool-name">check_usage</span><span class="tool-desc">Quota and cost tracking</span></div>
      <div class="tool"><span class="tool-name">health_check</span><span class="tool-desc">Server status and config</span></div>
    </div>
  </div>

  <div class="section">
    <h2>Pricing</h2>
    <div class="pricing">
      <div class="price-card"><div class="amount">$0.05</div><div class="label">1 provider hit</div></div>
      <div class="price-card"><div class="amount">$0.10</div><div class="label">2 providers hit</div></div>
      <div class="price-card"><div class="amount">$0.15</div><div class="label">3 providers hit</div></div>
    </div>
    <p style="color: #666; font-size: 0.8rem; margin-top: 0.75rem; text-align: center;">Free tier: {FREE_TIER_LIMIT} lookups/month</p>
  </div>

  <p class="footer">
    <a href="/health">Health Check</a> &middot;
    Built by <a href="https://freedomengineers.tech">Freedom Engineers</a>
  </p>
</div>
</body>
</html>""")


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "healthy",
        "service": "leadenrich",
        "version": "0.2.0",
        "providers": _configured_providers(),
        "cache": provider_cache_stats(),
    })


# ---------------------------------------------------------------------------
# Billing routes
# ---------------------------------------------------------------------------

@mcp.custom_route("/api/plans", methods=["GET"])
async def api_plans(request: Request) -> JSONResponse:
    return await handle_plans_route(request)


@mcp.custom_route("/api/checkout", methods=["POST"])
async def api_checkout(request: Request) -> JSONResponse:
    return await handle_checkout_route(request)


@mcp.custom_route("/api/webhook", methods=["POST"])
async def api_webhook(request: Request) -> JSONResponse:
    return await handle_webhook_route(request)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8300"))
    host = os.environ.get("HOST", "0.0.0.0")
    mcp.run(transport="streamable-http", host=host, port=port)
