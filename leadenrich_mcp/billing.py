"""Stripe billing integration for the LeadEnrich MCP server.

Handles subscription management, API key provisioning, and webhook processing.
Integrates with usage.py for tier-aware quota enforcement.

Environment variables:
    STRIPE_SECRET_KEY      — Stripe secret key (sk_live_... or sk_test_...)
    STRIPE_WEBHOOK_SECRET  — Stripe webhook signing secret (whsec_...)
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any

import stripe
from starlette.requests import Request
from starlette.responses import JSONResponse

from .usage import USAGE_DIR, get_usage, _usage_path, _current_month

log = logging.getLogger("leadenrich")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = "pk_live_51QmyYcKO5ZhyLZxIIsVBnMb6eOrERyVIkFkKHERSwGhKwJKlqI9oF4I7tNAsOSdeeO8vFiS5NMKssd0aIYC0hzu800FEeDeEWy"

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# ---------------------------------------------------------------------------
# Plan definitions
# ---------------------------------------------------------------------------

PLANS: dict[str, dict[str, Any]] = {
    "free": {
        "name": "Free",
        "price_cents": 0,
        "lookups_per_month": 50,
        "stripe_product_id": None,
        "stripe_price_id": None,
    },
    "starter": {
        "name": "Starter",
        "price_cents": 2900,
        "lookups_per_month": 500,
        "stripe_product_id": None,
        "stripe_price_id": None,
    },
    "pro": {
        "name": "Pro",
        "price_cents": 7900,
        "lookups_per_month": 2500,
        "stripe_product_id": None,
        "stripe_price_id": None,
    },
    "scale": {
        "name": "Scale",
        "price_cents": 19900,
        "lookups_per_month": 10000,
        "stripe_product_id": None,
        "stripe_price_id": None,
    },
}

# Lookup limits by tier (used by quota checks)
TIER_LIMITS: dict[str, int] = {
    tier: plan["lookups_per_month"] for tier, plan in PLANS.items()
}

# ---------------------------------------------------------------------------
# API key store — JSON file in USAGE_DIR
# ---------------------------------------------------------------------------

_KEYS_FILE = USAGE_DIR / "api_keys.json"


def _load_keys() -> dict[str, dict]:
    """Load the api_key -> metadata mapping from disk."""
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    if _KEYS_FILE.exists():
        try:
            return json.loads(_KEYS_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            log.warning("Corrupt api_keys.json — starting fresh")
    return {}


def _save_keys(keys: dict[str, dict]) -> None:
    """Persist the api_key -> metadata mapping."""
    USAGE_DIR.mkdir(parents=True, exist_ok=True)
    _KEYS_FILE.write_text(json.dumps(keys, indent=2))


def generate_api_key() -> str:
    """Generate a secure le_-prefixed API key."""
    return f"le_{secrets.token_urlsafe(32)}"


def provision_api_key(
    stripe_customer_id: str,
    stripe_subscription_id: str | None,
    tier: str,
    email: str | None = None,
) -> str:
    """Create a new API key and store its mapping. Returns the key."""
    keys = _load_keys()

    # Check if customer already has a key — return existing one
    for key, meta in keys.items():
        if meta.get("stripe_customer_id") == stripe_customer_id:
            # Update tier on existing key
            meta["tier"] = tier
            meta["stripe_subscription_id"] = stripe_subscription_id
            meta["updated_at"] = time.time()
            _save_keys(keys)
            _update_usage_tier(key, tier)
            log.info(f"Updated existing key for customer {stripe_customer_id} to tier={tier}")
            return key

    # New customer — generate a key
    api_key = generate_api_key()
    keys[api_key] = {
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "tier": tier,
        "email": email,
        "created_at": time.time(),
        "updated_at": time.time(),
        "payment_failed": False,
    }
    _save_keys(keys)
    _update_usage_tier(api_key, tier)
    log.info(f"Provisioned new API key for customer {stripe_customer_id}, tier={tier}")
    return api_key


def get_key_metadata(api_key: str) -> dict | None:
    """Get metadata for an API key, or None if not found."""
    keys = _load_keys()
    return keys.get(api_key)


def update_key_tier(stripe_customer_id: str, tier: str) -> str | None:
    """Update the tier for a customer's API key. Returns the key or None."""
    keys = _load_keys()
    for key, meta in keys.items():
        if meta.get("stripe_customer_id") == stripe_customer_id:
            meta["tier"] = tier
            meta["updated_at"] = time.time()
            meta["payment_failed"] = False
            _save_keys(keys)
            _update_usage_tier(key, tier)
            log.info(f"Updated tier for customer {stripe_customer_id} to {tier}")
            return key
    return None


def flag_payment_failed(stripe_customer_id: str) -> str | None:
    """Flag an account as having a failed payment. Returns the key or None."""
    keys = _load_keys()
    for key, meta in keys.items():
        if meta.get("stripe_customer_id") == stripe_customer_id:
            meta["payment_failed"] = True
            meta["updated_at"] = time.time()
            _save_keys(keys)
            log.warning(f"Payment failed for customer {stripe_customer_id}")
            return key
    return None


def _update_usage_tier(api_key: str, tier: str) -> None:
    """Update the tier field on the current month's usage record."""
    month = _current_month()
    record = get_usage(api_key, month)
    record.tier = tier

    path = _usage_path(api_key, month)
    path.write_text(json.dumps({
        "lookup_count": record.lookup_count,
        "cost_cents": record.cost_cents,
        "tier": tier,
        "updated_at": time.time(),
    }))


# ---------------------------------------------------------------------------
# Stripe product + price setup (run once)
# ---------------------------------------------------------------------------

def setup_stripe_products() -> dict[str, dict]:
    """Create Stripe products and prices for each paid tier.

    Idempotent: searches for existing products by metadata before creating.
    Returns a dict of tier -> {product_id, price_id}.
    """
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY not set")

    results: dict[str, dict] = {}

    for tier_key, plan in PLANS.items():
        if tier_key == "free":
            continue

        # Search for existing product by metadata
        existing = stripe.Product.search(
            query=f'metadata["leadenrich_tier"]:"{tier_key}"',
        )

        if existing.data:
            product = existing.data[0]
            log.info(f"Found existing product for {tier_key}: {product.id}")
        else:
            product = stripe.Product.create(
                name=f"LeadEnrich {plan['name']}",
                description=f"{plan['lookups_per_month']} lookups/month — LeadEnrich waterfall enrichment",
                metadata={"leadenrich_tier": tier_key},
            )
            log.info(f"Created product for {tier_key}: {product.id}")

        # Check for existing price
        prices = stripe.Price.list(product=product.id, active=True)
        matching_price = None
        for p in prices.data:
            if (
                p.unit_amount == plan["price_cents"]
                and p.recurring
                and p.recurring.interval == "month"
            ):
                matching_price = p
                break

        if matching_price:
            price = matching_price
            log.info(f"Found existing price for {tier_key}: {price.id}")
        else:
            price = stripe.Price.create(
                product=product.id,
                unit_amount=plan["price_cents"],
                currency="usd",
                recurring={"interval": "month"},
                metadata={"leadenrich_tier": tier_key},
            )
            log.info(f"Created price for {tier_key}: {price.id}")

        results[tier_key] = {
            "product_id": product.id,
            "price_id": price.id,
        }

        # Store IDs back on PLANS for runtime use
        PLANS[tier_key]["stripe_product_id"] = product.id
        PLANS[tier_key]["stripe_price_id"] = price.id

    return results


# ---------------------------------------------------------------------------
# Stripe checkout
# ---------------------------------------------------------------------------

def create_checkout_session(
    tier: str,
    success_url: str,
    cancel_url: str,
    customer_email: str | None = None,
) -> stripe.checkout.Session:
    """Create a Stripe checkout session for a given tier."""
    if not STRIPE_SECRET_KEY:
        raise RuntimeError("STRIPE_SECRET_KEY not set")

    if tier not in PLANS or tier == "free":
        raise ValueError(f"Invalid tier for checkout: {tier}. Use one of: starter, pro, scale")

    plan = PLANS[tier]
    price_id = plan.get("stripe_price_id")

    if not price_id:
        # Try to find the price from Stripe if not loaded yet
        products = stripe.Product.search(
            query=f'metadata["leadenrich_tier"]:"{tier}"',
        )
        if not products.data:
            raise ValueError(
                f"Stripe product not found for tier '{tier}'. "
                "Run setup_stripe_products() first."
            )
        prices = stripe.Price.list(product=products.data[0].id, active=True)
        if not prices.data:
            raise ValueError(f"No active price found for tier '{tier}'")
        price_id = prices.data[0].id
        PLANS[tier]["stripe_price_id"] = price_id
        PLANS[tier]["stripe_product_id"] = products.data[0].id

    params: dict[str, Any] = {
        "mode": "subscription",
        "line_items": [{"price": price_id, "quantity": 1}],
        "success_url": success_url,
        "cancel_url": cancel_url,
        "metadata": {"leadenrich_tier": tier},
        "subscription_data": {
            "metadata": {"leadenrich_tier": tier},
        },
    }
    if customer_email:
        params["customer_email"] = customer_email

    return stripe.checkout.Session.create(**params)


# ---------------------------------------------------------------------------
# Webhook processing
# ---------------------------------------------------------------------------

def _tier_from_subscription(subscription: stripe.Subscription) -> str:
    """Extract the LeadEnrich tier from a subscription's metadata or line items."""
    # Check subscription metadata first
    tier = subscription.metadata.get("leadenrich_tier")
    if tier and tier in PLANS:
        return tier

    # Fall back to checking price/product metadata on line items
    for item in subscription.get("items", {}).get("data", []):
        price = item.get("price", {})
        tier = price.get("metadata", {}).get("leadenrich_tier")
        if tier and tier in PLANS:
            return tier
        # Check the product
        product_id = price.get("product")
        if product_id:
            try:
                product = stripe.Product.retrieve(product_id)
                tier = product.metadata.get("leadenrich_tier")
                if tier and tier in PLANS:
                    return tier
            except Exception:
                pass

    return "free"


def handle_checkout_completed(session: stripe.checkout.Session) -> dict:
    """Handle checkout.session.completed — provision API key and set tier."""
    customer_id = session.customer
    subscription_id = session.subscription
    email = session.customer_email or session.customer_details.get("email") if session.customer_details else None
    tier = session.metadata.get("leadenrich_tier", "starter")

    api_key = provision_api_key(
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        tier=tier,
        email=email,
    )

    return {
        "event": "checkout.session.completed",
        "customer_id": customer_id,
        "tier": tier,
        "api_key_prefix": api_key[:8] + "...",
    }


def handle_subscription_updated(subscription: stripe.Subscription) -> dict:
    """Handle customer.subscription.updated — update tier."""
    customer_id = subscription.customer
    tier = _tier_from_subscription(subscription)

    key = update_key_tier(customer_id, tier)

    return {
        "event": "customer.subscription.updated",
        "customer_id": customer_id,
        "tier": tier,
        "key_found": key is not None,
    }


def handle_subscription_deleted(subscription: stripe.Subscription) -> dict:
    """Handle customer.subscription.deleted — downgrade to free."""
    customer_id = subscription.customer

    key = update_key_tier(customer_id, "free")

    return {
        "event": "customer.subscription.deleted",
        "customer_id": customer_id,
        "tier": "free",
        "key_found": key is not None,
    }


def handle_payment_failed(invoice: stripe.Invoice) -> dict:
    """Handle invoice.payment_failed — flag account."""
    customer_id = invoice.customer

    key = flag_payment_failed(customer_id)

    return {
        "event": "invoice.payment_failed",
        "customer_id": customer_id,
        "key_found": key is not None,
    }


# ---------------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------------

async def handle_plans_route(request: Request) -> JSONResponse:
    """GET /api/plans — return available plans."""
    plans_out = {}
    for tier_key, plan in PLANS.items():
        plans_out[tier_key] = {
            "name": plan["name"],
            "price_usd": f"${plan['price_cents'] / 100:.0f}" if plan["price_cents"] > 0 else "Free",
            "price_cents": plan["price_cents"],
            "lookups_per_month": plan["lookups_per_month"],
        }
    return JSONResponse({
        "plans": plans_out,
        "publishable_key": STRIPE_PUBLISHABLE_KEY,
    })


async def handle_checkout_route(request: Request) -> JSONResponse:
    """POST /api/checkout — create a Stripe checkout session."""
    if not STRIPE_SECRET_KEY:
        return JSONResponse(
            {"error": "Billing not configured"},
            status_code=503,
        )

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    tier = body.get("tier")
    if not tier or tier not in PLANS or tier == "free":
        return JSONResponse(
            {"error": f"Invalid tier. Choose one of: starter, pro, scale"},
            status_code=400,
        )

    success_url = body.get("success_url", "https://freedomengineers.tech/leadenrich/success?session_id={CHECKOUT_SESSION_ID}")
    cancel_url = body.get("cancel_url", "https://freedomengineers.tech/leadenrich/pricing")
    customer_email = body.get("email")

    try:
        session = create_checkout_session(
            tier=tier,
            success_url=success_url,
            cancel_url=cancel_url,
            customer_email=customer_email,
        )
        return JSONResponse({
            "checkout_url": session.url,
            "session_id": session.id,
        })
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except stripe.StripeError as e:
        log.error(f"Stripe error creating checkout: {e}")
        return JSONResponse({"error": "Payment service error"}, status_code=502)


async def handle_webhook_route(request: Request) -> JSONResponse:
    """POST /api/webhook — Stripe webhook endpoint."""
    payload = await request.body()

    if STRIPE_WEBHOOK_SECRET:
        sig_header = request.headers.get("stripe-signature", "")
        try:
            event = stripe.Webhook.construct_event(
                payload, sig_header, STRIPE_WEBHOOK_SECRET,
            )
        except stripe.SignatureVerificationError:
            log.warning("Webhook signature verification failed")
            return JSONResponse({"error": "Invalid signature"}, status_code=400)
        except Exception as e:
            log.error(f"Webhook construct error: {e}")
            return JSONResponse({"error": "Webhook error"}, status_code=400)
    else:
        # No webhook secret configured — parse raw (dev mode only)
        log.warning("STRIPE_WEBHOOK_SECRET not set — skipping signature verification")
        try:
            event = stripe.Event.construct_from(
                json.loads(payload), stripe.api_key,
            )
        except Exception as e:
            log.error(f"Webhook parse error: {e}")
            return JSONResponse({"error": "Invalid payload"}, status_code=400)

    event_type = event.type
    log.info(f"Stripe webhook received: {event_type}")

    try:
        if event_type == "checkout.session.completed":
            result = handle_checkout_completed(event.data.object)
        elif event_type == "customer.subscription.updated":
            result = handle_subscription_updated(event.data.object)
        elif event_type == "customer.subscription.deleted":
            result = handle_subscription_deleted(event.data.object)
        elif event_type == "invoice.payment_failed":
            result = handle_payment_failed(event.data.object)
        else:
            log.info(f"Unhandled webhook event type: {event_type}")
            return JSONResponse({"status": "ignored", "type": event_type})

        log.info(f"Webhook handled: {result}")
        return JSONResponse({"status": "ok", "result": result})

    except Exception as e:
        log.error(f"Webhook handler error for {event_type}: {e}", exc_info=True)
        return JSONResponse({"error": "Internal handler error"}, status_code=500)


# ---------------------------------------------------------------------------
# Quota check integration — replaces simple free-tier check
# ---------------------------------------------------------------------------

def get_tier_limit(tier: str) -> int:
    """Get the lookup limit for a tier."""
    return TIER_LIMITS.get(tier, 50)


def check_quota_with_tier(api_key: str) -> tuple[bool, "UsageRecord"]:
    """Enhanced quota check that respects tier-based limits.

    Import and call this instead of usage.check_quota for tier-aware enforcement.
    """
    record = get_usage(api_key)

    # Check key metadata for tier override
    meta = get_key_metadata(api_key)
    if meta:
        record.tier = meta.get("tier", "free")
        if meta.get("payment_failed"):
            # Payment failed — keep current tier but warn
            pass

    limit = get_tier_limit(record.tier)
    return record.lookup_count < limit, record
