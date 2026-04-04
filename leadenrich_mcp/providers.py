"""Enrichment provider integrations — Apollo, Clearbit, Hunter.

Uses a shared httpx client with connection pooling. Each provider function
accepts the client as a parameter to avoid creating/destroying connections
per request.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger("leadenrich")

TIMEOUT = 15

# ---------------------------------------------------------------------------
# Shared HTTP client (created lazily, reused across requests)
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


# ---------------------------------------------------------------------------
# Result cache — avoid burning API credits on duplicate lookups
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
CACHE_TTL = 3600  # 1 hour


def _cache_key(provider: str, **kwargs: Any) -> str:
    parts = f"{provider}:" + "|".join(f"{k}={v}" for k, v in sorted(kwargs.items()) if v)
    return hashlib.sha256(parts.encode()).hexdigest()[:16]


def _cache_get(key: str) -> dict[str, Any] | None:
    entry = _cache.get(key)
    if entry and (time.time() - entry[0]) < CACHE_TTL:
        return entry[1]
    if entry:
        del _cache[key]
    return None


def _cache_set(key: str, data: dict[str, Any]) -> None:
    # Cap cache size to prevent unbounded growth
    if len(_cache) > 5000:
        cutoff = time.time() - CACHE_TTL
        expired = [k for k, (t, _) in _cache.items() if t < cutoff]
        for k in expired:
            del _cache[k]
    _cache[key] = (time.time(), data)


# ---------------------------------------------------------------------------
# EnrichResult
# ---------------------------------------------------------------------------

@dataclass
class EnrichResult:
    """Normalized enrichment data from any provider."""

    provider: str
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    full_name: str | None = None
    title: str | None = None
    company: str | None = None
    company_domain: str | None = None
    linkedin_url: str | None = None
    phone: str | None = None
    location: str | None = None
    industry: str | None = None
    company_size: str | None = None
    company_revenue: str | None = None
    company_founded: str | None = None
    company_description: str | None = None
    twitter_url: str | None = None
    confidence: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    _SKIP = {"provider", "raw", "confidence"}

    def filled_fields(self) -> int:
        return sum(
            1 for k, v in self.__dict__.items()
            if k not in self._SKIP and not k.startswith("_") and v is not None
        )

    def to_dict(self) -> dict[str, Any]:
        d = {
            k: v for k, v in self.__dict__.items()
            if k != "raw" and not k.startswith("_") and v is not None
        }
        d["filled_fields"] = self.filled_fields()
        return d


def merge_results(results: list[EnrichResult]) -> dict[str, Any]:
    """Merge multiple EnrichResults, preferring earlier (higher-priority) providers.

    Also picks the highest confidence score across all providers.
    """
    merged: dict[str, Any] = {}
    providers_used: list[str] = []
    fields_by_provider: dict[str, list[str]] = {}
    best_confidence = 0.0

    skip = {"provider", "raw", "confidence"}

    for r in results:
        providers_used.append(r.provider)
        best_confidence = max(best_confidence, r.confidence)
        contributed: list[str] = []
        for k, v in r.__dict__.items():
            if k in skip or k.startswith("_") or v is None:
                continue
            if k not in merged:
                merged[k] = v
                contributed.append(k)
        fields_by_provider[r.provider] = contributed

    merged["confidence"] = round(best_confidence, 2)
    merged["providers_used"] = providers_used
    merged["fields_by_provider"] = fields_by_provider
    merged["total_fields"] = len(
        [k for k in merged if k not in {"providers_used", "fields_by_provider", "total_fields", "confidence"}]
    )
    return merged


# ---------------------------------------------------------------------------
# Apollo
# ---------------------------------------------------------------------------

async def enrich_apollo(
    api_key: str,
    email: str | None = None,
    domain: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> EnrichResult | None:
    if not api_key:
        return None

    ck = _cache_key("apollo", email=email, domain=domain, first_name=first_name, last_name=last_name)
    cached = _cache_get(ck)

    payload: dict[str, Any] = {}
    if email:
        payload["email"] = email
    if domain:
        payload["domain"] = domain
    if first_name:
        payload["first_name"] = first_name
    if last_name:
        payload["last_name"] = last_name
    if not payload:
        return None

    if cached:
        person = cached
    else:
        headers = {
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "X-Api-Key": api_key,
        }
        try:
            client = await get_client()
            resp = await client.post(
                "https://api.apollo.io/v1/people/match",
                json=payload, headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("Apollo HTTP %d: %s", e.response.status_code, e.response.text[:200])
            return None
        except httpx.HTTPError as e:
            log.warning("Apollo request failed: %s", e)
            return None

        person = data.get("person") or data
        if not person or person.get("id") is None:
            return None
        _cache_set(ck, person)

    org = person.get("organization") or {}

    return EnrichResult(
        provider="apollo",
        email=person.get("email"),
        first_name=person.get("first_name"),
        last_name=person.get("last_name"),
        full_name=person.get("name"),
        title=person.get("title"),
        company=org.get("name"),
        company_domain=org.get("primary_domain"),
        linkedin_url=person.get("linkedin_url"),
        phone=person.get("phone_number") or None,
        location=_build_location(person.get("city"), person.get("state"), person.get("country")),
        industry=org.get("industry"),
        company_size=str(org["estimated_num_employees"]) if org.get("estimated_num_employees") else None,
        company_revenue=org.get("annual_revenue_printed"),
        company_founded=str(org["founded_year"]) if org.get("founded_year") else None,
        company_description=org.get("short_description"),
        twitter_url=person.get("twitter_url"),
        confidence=0.9 if person.get("email") else 0.7,
        raw=person,
    )


# ---------------------------------------------------------------------------
# Clearbit
# ---------------------------------------------------------------------------

async def enrich_clearbit(
    api_key: str,
    email: str | None = None,
    domain: str | None = None,
) -> EnrichResult | None:
    if not api_key:
        return None

    ck = _cache_key("clearbit", email=email, domain=domain)
    cached = _cache_get(ck)

    if cached:
        data = cached
    else:
        if email:
            url = "https://person.clearbit.com/v2/combined/find"
            params = {"email": email}
        elif domain:
            url = "https://company.clearbit.com/v2/companies/find"
            params = {"domain": domain}
        else:
            return None

        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            client = await get_client()
            resp = await client.get(url, params=params, headers=headers)
            if resp.status_code in (404, 422):
                return None
            if resp.status_code == 202:
                return None  # async processing, no data yet
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            log.warning("Clearbit HTTP %d: %s", e.response.status_code, e.response.text[:200])
            return None
        except httpx.HTTPError as e:
            log.warning("Clearbit request failed: %s", e)
            return None

        _cache_set(ck, data)

    person = data.get("person") or {}
    company = data.get("company") or data

    name_data = person.get("name") or {}
    metrics = company.get("metrics") or {}
    emp = metrics.get("employees")
    rev = metrics.get("estimatedAnnualRevenue")
    category = company.get("category") or {}

    linkedin_handle = None
    if person.get("linkedin"):
        lh = person["linkedin"].get("handle")
        if lh:
            linkedin_handle = f"https://linkedin.com/{lh}" if not lh.startswith("http") else lh

    return EnrichResult(
        provider="clearbit",
        email=person.get("email"),
        first_name=name_data.get("givenName"),
        last_name=name_data.get("familyName"),
        full_name=name_data.get("fullName"),
        title=(person.get("employment") or {}).get("title"),
        company=company.get("name"),
        company_domain=company.get("domain"),
        linkedin_url=linkedin_handle,
        phone=company.get("phone"),
        location=person.get("location") or company.get("location"),
        industry=category.get("industry"),
        company_size=str(emp) if emp else None,
        company_revenue=rev,
        company_founded=str(company["foundedYear"]) if company.get("foundedYear") else None,
        company_description=company.get("description"),
        twitter_url=f"https://twitter.com/{company['twitter']['handle']}" if company.get("twitter", {}).get("handle") else None,
        confidence=0.85 if person.get("email") else 0.6,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Hunter.io
# ---------------------------------------------------------------------------

async def enrich_hunter(
    api_key: str,
    email: str | None = None,
    domain: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> EnrichResult | None:
    if not api_key:
        return None

    ck = _cache_key("hunter", email=email, domain=domain, first_name=first_name, last_name=last_name)
    cached = _cache_get(ck)

    if cached:
        data = cached
    else:
        if email:
            url = "https://api.hunter.io/v2/email-verifier"
            params: dict[str, str] = {"email": email, "api_key": api_key}
        elif domain and first_name and last_name:
            url = "https://api.hunter.io/v2/email-finder"
            params = {
                "domain": domain,
                "first_name": first_name,
                "last_name": last_name,
                "api_key": api_key,
            }
        elif domain:
            url = "https://api.hunter.io/v2/domain-search"
            params = {"domain": domain, "api_key": api_key, "limit": "1"}
        else:
            return None

        try:
            client = await get_client()
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json().get("data", {})
        except httpx.HTTPStatusError as e:
            log.warning("Hunter HTTP %d: %s", e.response.status_code, e.response.text[:200])
            return None
        except httpx.HTTPError as e:
            log.warning("Hunter request failed: %s", e)
            return None

        if not data:
            return None
        _cache_set(ck, data)

    found_email = data.get("email")
    score = data.get("score") or data.get("confidence")

    return EnrichResult(
        provider="hunter",
        email=found_email,
        first_name=data.get("first_name") or first_name,
        last_name=data.get("last_name") or last_name,
        company=data.get("organization"),
        company_domain=data.get("domain") or domain,
        industry=data.get("industry"),
        linkedin_url=data.get("linkedin"),
        twitter_url=data.get("twitter"),
        confidence=float(score) / 100 if score else 0.5,
        raw=data,
    )


# ---------------------------------------------------------------------------
# Waterfall orchestrator
# ---------------------------------------------------------------------------

async def waterfall_enrich(
    apollo_key: str,
    clearbit_key: str,
    hunter_key: str,
    email: str | None = None,
    domain: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    providers: list[str] | None = None,
) -> tuple[dict[str, Any], int]:
    """Smart waterfall enrichment.

    - If we have email: run all providers concurrently (no dependency chain).
    - If we only have domain/name: run Apollo first to discover email,
      then run Clearbit + Hunter concurrently with the discovered email.

    Returns (merged_data, providers_hit_count).
    """
    allowed = set(providers or ["apollo", "clearbit", "hunter"])
    results: list[EnrichResult] = []

    if email:
        # We have the best identifier — run all providers in parallel
        tasks = []
        if "apollo" in allowed and apollo_key:
            tasks.append(enrich_apollo(apollo_key, email=email, domain=domain,
                                       first_name=first_name, last_name=last_name))
        if "clearbit" in allowed and clearbit_key:
            tasks.append(enrich_clearbit(clearbit_key, email=email, domain=domain))
        if "hunter" in allowed and hunter_key:
            tasks.append(enrich_hunter(hunter_key, email=email, domain=domain,
                                       first_name=first_name, last_name=last_name))

        if tasks:
            raw = await asyncio.gather(*tasks, return_exceptions=True)
            for r in raw:
                if isinstance(r, EnrichResult) and r.filled_fields() > 0:
                    results.append(r)
                elif isinstance(r, Exception):
                    log.warning("Provider error during parallel enrich: %s", r)

    else:
        # No email — sequential waterfall to discover it
        # Stage 1: Apollo (best at finding people by domain + name)
        if "apollo" in allowed and apollo_key:
            r = await enrich_apollo(apollo_key, email=None, domain=domain,
                                    first_name=first_name, last_name=last_name)
            if r and r.filled_fields() > 0:
                results.append(r)
                if r.email:
                    email = r.email
                if r.company_domain and not domain:
                    domain = r.company_domain

        # Stage 2+3: Clearbit + Hunter can run concurrently now
        tasks = []
        if "clearbit" in allowed and clearbit_key:
            tasks.append(enrich_clearbit(clearbit_key, email=email, domain=domain))
        if "hunter" in allowed and hunter_key:
            tasks.append(enrich_hunter(hunter_key, email=email, domain=domain,
                                       first_name=first_name, last_name=last_name))
        if tasks:
            raw = await asyncio.gather(*tasks, return_exceptions=True)
            for r in raw:
                if isinstance(r, EnrichResult) and r.filled_fields() > 0:
                    results.append(r)
                elif isinstance(r, Exception):
                    log.warning("Provider error during waterfall: %s", r)

    if not results:
        return {"error": "No enrichment data found from any provider"}, 0

    merged = merge_results(results)
    return merged, len(results)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_location(*parts: str | None) -> str | None:
    filtered = [p for p in parts if p]
    return ", ".join(filtered) if filtered else None


def cache_stats() -> dict[str, Any]:
    """Return cache statistics."""
    now = time.time()
    valid = sum(1 for t, _ in _cache.values() if (now - t) < CACHE_TTL)
    return {"total_entries": len(_cache), "valid_entries": valid, "ttl_seconds": CACHE_TTL}


def clear_cache() -> int:
    """Clear all cached results. Returns count of cleared entries."""
    count = len(_cache)
    _cache.clear()
    return count
