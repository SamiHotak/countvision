"""Billing: free trial, Stripe subscriptions, plan limits, operator grants.

Rules
- Billing is OFF when no Stripe key is set (CV_STRIPE_SECRET_KEY): no limits for anyone.
- Every organization starts with a free trial (CV_TRIAL_DAYS, default 14; no card).
- Paid plans live in Stripe. The ``org_billing`` row is a copy that webhooks keep up to date; when a
  webhook is late (or ``stripe listen`` is not running), "sync" reads the state from Stripe directly.
  We always re-read the subscription from Stripe instead of trusting the event body, so events that
  arrive twice or in the wrong order cannot leave a wrong state.
- The operator (platform admin) can extend a trial or grant a plan until a date (pilot customers).
- Which plan counts, strongest first: grant -> active/past_due subscription -> trial -> nothing.
- No plan: data already flowing is still stored (nothing is lost), but the product pages and the
  public API answer 402 until a plan is chosen, and alerts/reports/webhooks pause.

Product code plugs in how usage is measured (``register_usage``) and what happens when the plan
changes (``register_plan_changed``), so this module stays product-neutral.
"""

from __future__ import annotations

import json
import logging
import math
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ...db import utcnow
from ...redis_client import get_redis
from ...settings import get_settings
from ..errors import AppError, Conflict, PaymentRequired
from ..models import Organization, OrgBilling, StripeEvent, User
from ..plans import LIMIT_LABELS, NONE, PLANS, TRIAL, UNLIMITED, Plan, get_plan, plan_by_lookup_key
from . import audit, stripe_api

log = logging.getLogger(__name__)

ACTIVE_SUB = ("active", "trialing", "past_due")  # Stripe statuses that keep the plan working
MIN_STRIPE_TRIAL = timedelta(hours=49)  # Stripe needs trial_end at least 48 h in the future
PORTAL_CACHE_KEY = "cv:billing:portal_config"

# --- hooks for the product ----------------------------------------------------------------------

_usage_fns: list[Callable[[Session, uuid.UUID], dict[str, int]]] = []
_changed_fns: list[Callable[[Session, uuid.UUID], None]] = []


def register_usage(fn: Callable[[Session, uuid.UUID], dict[str, int]]) -> None:
    """fn(db, org_id) -> {"cameras": 3, ...}: what the organization uses now."""
    if fn not in _usage_fns:
        _usage_fns.append(fn)


def register_plan_changed(fn: Callable[[Session, uuid.UUID], None]) -> None:
    """fn(db, org_id): called inside the transaction after the effective plan may have changed."""
    if fn not in _changed_fns:
        _changed_fns.append(fn)


def usage(db: Session, org_id: uuid.UUID) -> dict[str, int]:
    out: dict[str, int] = {}
    for fn in _usage_fns:
        out.update(fn(db, org_id))
    return out


def plan_changed(db: Session, org_id: uuid.UUID) -> None:
    for fn in _changed_fns:
        fn(db, org_id)


# --- state ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Entitlement:
    """What an organization may do right now."""

    state: str  # off | trial | active | past_due | comp | expired | canceled
    plan: Plan
    active: bool
    trial_ends_at: datetime | None = None
    period_end: datetime | None = None
    cancel_at_period_end: bool = False
    paid_from: datetime | None = None  # rest of the free trial carried into Stripe
    comp_until: datetime | None = None
    comp_note: str | None = None

    def limit(self, name: str) -> int:
        return self.plan.limit(name)


def billing_row(db: Session, org_id: uuid.UUID, *, lock: bool = False) -> OrgBilling:
    """The billing row of an organization; created with a fresh trial if it is missing."""
    stmt = select(OrgBilling).where(OrgBilling.org_id == org_id)
    if lock:
        stmt = stmt.with_for_update()
    row = db.scalar(stmt)
    if row is None:
        db.execute(
            insert(OrgBilling)
            .values(org_id=org_id, trial_ends_at=utcnow() + timedelta(days=get_settings().trial_days))
            .on_conflict_do_nothing(index_elements=["org_id"])
        )
        row = db.scalar(stmt)
        assert row is not None
    return row


def start_trial(db: Session, org: Organization) -> OrgBilling:
    """Called when an organization is created."""
    row = OrgBilling(org_id=org.id, trial_ends_at=utcnow() + timedelta(days=get_settings().trial_days))
    db.add(row)
    return row


def entitlement_of(row: OrgBilling | None, now: datetime | None = None) -> Entitlement:
    """Pure function: the effective plan from the stored state (see the module docstring)."""
    if not get_settings().billing_enabled:
        return Entitlement("off", UNLIMITED, True)
    now = now or utcnow()
    if row is None:
        return Entitlement(
            "trial", TRIAL, True, trial_ends_at=now + timedelta(days=get_settings().trial_days)
        )
    common = dict(
        trial_ends_at=row.trial_ends_at,
        comp_until=row.comp_until,
        comp_note=row.comp_note,
        period_end=row.current_period_end,
        cancel_at_period_end=row.cancel_at_period_end,
    )
    comp = get_plan(row.comp_plan)
    if comp and row.comp_until and row.comp_until > now:
        return Entitlement("comp", comp, True, **common)
    paid = get_plan(row.plan)
    if paid and row.sub_status in ACTIVE_SUB:
        paid_from = row.sub_trial_end if row.sub_status == "trialing" else None
        state = "past_due" if row.sub_status == "past_due" else "active"
        return Entitlement(state, paid, True, paid_from=paid_from, **common)
    if row.trial_ends_at > now:
        return Entitlement("trial", TRIAL, True, **common)
    return Entitlement("canceled" if row.stripe_subscription_id else "expired", NONE, False, **common)


def entitlement(db: Session, org_id: uuid.UUID) -> Entitlement:
    settings = get_settings()
    if settings.is_local:  # CountVision Local: a licence key instead of Stripe (one per installation)
        from ...local import licence

        return licence.entitlement(db)
    if not settings.billing_enabled:
        return Entitlement("off", UNLIMITED, True)
    return entitlement_of(billing_row(db, org_id))


def require_active(db: Session, org_id: uuid.UUID) -> Entitlement:
    ent = entitlement(db, org_id)
    if not ent.active:
        if get_settings().is_local:
            why = "Your free trial has ended." if ent.state == "expired" else "Your licence has ended."
            raise PaymentRequired(
                f"{why} Add a licence key on the Licence page to keep using CountVision. Your data is safe.",
                code="licence_required",
            )
        why = "Your free trial has ended." if ent.state == "expired" else "Your subscription has ended."
        raise PaymentRequired(
            f"{why} Choose a plan on the Billing page to keep using CountVision. Your data is safe.",
            code="subscription_required",
        )
    return ent


def check_limit(
    db: Session, org_id: uuid.UUID, name: str, *, adding: int = 1, used: int | None = None
) -> Entitlement:
    """Raise 402 plan_limit if ``used + adding`` would go over the plan's limit."""
    ent = require_active(db, org_id)
    limit = ent.limit(name)
    if limit < 0:
        return ent
    used = usage(db, org_id).get(name, 0) if used is None else used
    if used + adding > limit:
        label = LIMIT_LABELS.get(name, name)
        where = ("A bigger licence key (Licence page)" if get_settings().is_local
                 else "Upgrade on the Billing page")
        raise PaymentRequired(
            f"Your plan ({ent.plan.name}) includes {limit} {label} and you use {used}. "
            f"{where}, or free some up first.",
            code="plan_limit",
            fields={"limit": str(limit), "used": str(used), "name": name},
        )
    return ent


def inactive_org_ids(db: Session) -> set[uuid.UUID]:
    """Organizations without a working plan (alerts, reports and webhooks skip them)."""
    settings = get_settings()
    if settings.is_local:
        from ...local import licence

        return set() if licence.entitlement(db).active else licence.all_org_ids(db)
    if not settings.billing_enabled:
        return set()
    now = utcnow()
    return {row.org_id for row in db.scalars(select(OrgBilling)) if not entitlement_of(row, now).active}


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def plan_out(plan: Plan) -> dict[str, Any]:
    return {
        "id": plan.id,
        "name": plan.name,
        "price_eur": plan.price_eur,
        "limits": plan.limits,
        "blurb": plan.blurb,
    }


def summary(db: Session, org_id: uuid.UUID) -> dict[str, Any]:
    """Everything the Billing page needs (and a short version for the banner)."""
    settings = get_settings()
    ent = entitlement(db, org_id)
    row = billing_row(db, org_id) if settings.billing_enabled else None
    now = utcnow()
    days_left = None
    if ent.state == "trial" and ent.trial_ends_at:
        days_left = max(0, math.ceil((ent.trial_ends_at - now).total_seconds() / 86400))
    return {
        "enabled": settings.billing_enabled,
        "test_mode": settings.stripe_test_mode,
        "state": ent.state,
        "active": ent.active,
        "plan": plan_out(ent.plan),
        "subscribed_plan": row.plan if row and row.sub_status in ACTIVE_SUB else None,
        "trial_ends_at": _iso(ent.trial_ends_at),
        "trial_days_left": days_left,
        "period_end": _iso(ent.period_end),
        "cancel_at_period_end": ent.cancel_at_period_end,
        "paid_from": _iso(ent.paid_from),
        "comp_until": _iso(ent.comp_until) if ent.state == "comp" else None,
        "comp_note": ent.comp_note if ent.state == "comp" else None,
        "has_customer": bool(row and row.stripe_customer_id),
        "usage": usage(db, org_id),
        "plans": [plan_out(p) for p in PLANS.values()],
        "trial_plan": plan_out(TRIAL),
        "synced_at": _iso(row.synced_at) if row else None,
    }


# --- Stripe: checkout, portal, sync ---------------------------------------------------------------


def _ensure_customer(db: Session, org: Organization, row: OrgBilling, user: User) -> str:
    if row.stripe_customer_id:
        return row.stripe_customer_id
    cust = stripe_api.post(
        "/v1/customers",
        {
            "email": user.email,
            "name": org.name,
            "metadata": {"org_id": str(org.id), "app": "countvision"},
        },
        idempotency_key=f"countvision-customer-{org.id}",
    )
    row.stripe_customer_id = cust["id"]
    db.flush()
    return cust["id"]


def price_id(plan: Plan) -> str:
    found = stripe_api.get("/v1/prices", {"lookup_keys": [plan.lookup_key], "active": True, "limit": 1})
    data = found.get("data") or []
    if not data:
        raise stripe_api.StripeError(
            f"The Stripe price for {plan.name} is missing. Run 'countvision-cloud stripe-setup' once.",
            code="stripe_not_set_up",
            status=409,
        )
    return data[0]["id"]


def checkout(db: Session, org: Organization, user: User, plan_id: str) -> str:
    """Create a Stripe Checkout page for a plan; returns its URL (the browser goes there)."""
    plan = get_plan(plan_id)
    if plan is None:
        raise AppError("Unknown plan.", code="unknown_plan", fields={"plan": "Unknown plan"})
    row = billing_row(db, org.id, lock=True)
    if row.sub_status in ACTIVE_SUB:
        raise Conflict(
            "You already have a subscription. Use 'Manage billing' to change the plan.",
            code="already_subscribed",
        )
    customer = _ensure_customer(db, org, row, user)
    web = get_settings().web_url
    params: dict[str, Any] = {
        "mode": "subscription",
        "customer": customer,
        "client_reference_id": str(org.id),
        "line_items": [{"price": price_id(plan), "quantity": 1}],
        "success_url": f"{web}/app/orgs/{org.id}/billing?checkout=done",
        "cancel_url": f"{web}/app/orgs/{org.id}/billing?checkout=cancelled",
        "allow_promotion_codes": True,
        "billing_address_collection": "required",
        "customer_update": {"address": "auto", "name": "auto"},
        "tax_id_collection": {"enabled": True},
        "metadata": {"org_id": str(org.id), "plan": plan.id},
        "subscription_data": {"metadata": {"org_id": str(org.id), "plan": plan.id}},
    }
    # Rest of the free trial: the card is charged only when the trial would have ended.
    now = utcnow()
    if row.trial_ends_at - now > MIN_STRIPE_TRIAL and not row.stripe_subscription_id:
        params["subscription_data"]["trial_end"] = int(row.trial_ends_at.timestamp())
    session = stripe_api.post("/v1/checkout/sessions", params)
    audit.record(
        db,
        "billing.checkout_started",
        actor=user.id,
        org_id=org.id,
        target_type="org",
        target_id=org.id,
        plan=plan.id,
    )
    return session["url"]


def _portal_config_id() -> str | None:
    r = get_redis()
    cached = r.get(PORTAL_CACHE_KEY)
    if cached:
        return cached.decode() if isinstance(cached, bytes) else cached
    found = stripe_api.get("/v1/billing_portal/configurations", {"active": True, "limit": 50})
    for cfg in found.get("data") or []:
        if (cfg.get("metadata") or {}).get("app") == "countvision":
            r.set(PORTAL_CACHE_KEY, cfg["id"], ex=3600)
            return cfg["id"]
    return None


def portal(db: Session, org: Organization, user: User) -> str:
    """Stripe's customer portal: change plan, card, address, invoices, cancel."""
    row = billing_row(db, org.id)
    if not row.stripe_customer_id:
        raise Conflict("There is nothing to manage yet. Choose a plan first.", code="no_customer")
    params: dict[str, Any] = {
        "customer": row.stripe_customer_id,
        "return_url": f"{get_settings().web_url}/app/orgs/{org.id}/billing?portal=back",
    }
    config = _portal_config_id()
    if config:
        params["configuration"] = config
    session = stripe_api.post("/v1/billing_portal/sessions", params)
    audit.record(
        db, "billing.portal_opened", actor=user.id, org_id=org.id, target_type="org", target_id=org.id
    )
    return session["url"]


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), tz=UTC) if value else None


def _best(subs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The subscription that decides the plan: a working one first, else the newest."""
    if not subs:
        return None
    return sorted(subs, key=lambda s: (s.get("status") in ACTIVE_SUB, s.get("created") or 0))[-1]


def apply_subscription(
    db: Session, row: OrgBilling, sub: dict[str, Any] | None, *, actor: uuid.UUID | None = None
) -> bool:
    """Copy a Stripe subscription into the billing row. Returns True if the plan state changed."""
    before = (row.plan, row.sub_status, row.cancel_at_period_end)
    if sub is None:
        row.sub_status = "canceled" if row.stripe_subscription_id else None
    else:
        items = (sub.get("items") or {}).get("data") or [{}]
        item = items[0]
        price = item.get("price") or {}
        plan = (
            plan_by_lookup_key(price.get("lookup_key"))
            or get_plan((price.get("metadata") or {}).get("plan"))
            or get_plan((sub.get("metadata") or {}).get("plan"))
        )
        if plan is None:
            log.warning(
                "Stripe subscription %s: price %s is not a CountVision plan", sub.get("id"), price.get("id")
            )
        row.stripe_subscription_id = sub.get("id")
        row.plan = plan.id if plan else None
        row.sub_status = sub.get("status")
        # Since API version 2025-03-31 the period end is on the item; older versions: on the subscription.
        row.current_period_end = _ts(item.get("current_period_end") or sub.get("current_period_end"))
        row.cancel_at_period_end = bool(sub.get("cancel_at_period_end") or sub.get("cancel_at"))
        row.sub_trial_end = _ts(sub.get("trial_end"))
    row.synced_at = utcnow()
    after = (row.plan, row.sub_status, row.cancel_at_period_end)
    if before == after:
        return False
    audit.record(
        db,
        "billing.subscription_changed",
        actor=actor,
        org_id=row.org_id,
        target_type="org",
        target_id=row.org_id,
        old_plan=before[0],
        new_plan=after[0],
        old_status=before[1],
        new_status=after[1],
        cancel_at_period_end=after[2],
    )
    plan_changed(db, row.org_id)
    return True


def sync(db: Session, row: OrgBilling, *, actor: uuid.UUID | None = None) -> bool:
    """Read this customer's subscriptions from Stripe and store the one that counts."""
    if not row.stripe_customer_id:
        return False
    found = stripe_api.get(
        "/v1/subscriptions", {"customer": row.stripe_customer_id, "status": "all", "limit": 20}
    )
    return apply_subscription(db, row, _best(found.get("data") or []), actor=actor)


# --- webhooks -----------------------------------------------------------------------------------


def _customer_id(obj: dict[str, Any]) -> str | None:
    cust = obj.get("customer")
    if isinstance(cust, dict):
        return cust.get("id")
    if obj.get("object") == "customer":
        return obj.get("id")
    return cust


def _org_hint(obj: dict[str, Any]) -> uuid.UUID | None:
    for value in (obj.get("client_reference_id"), (obj.get("metadata") or {}).get("org_id")):
        try:
            return uuid.UUID(str(value))
        except ValueError:
            continue
    return None


HANDLED_PREFIXES = ("checkout.session.", "customer.subscription.", "invoice.", "customer.")


def handle_webhook(db: Session, payload: bytes, signature: str | None) -> dict[str, Any]:
    """Verify, store once, and apply one Stripe event. Errors -> exception -> Stripe retries later."""
    secret = get_settings().stripe_webhook_secret
    if not secret:
        raise AppError(
            "CV_STRIPE_WEBHOOK_SECRET is not set on the server.", code="webhook_not_set_up", status=503
        )
    stripe_api.verify(payload, signature, secret)
    event = json.loads(payload)
    event_id, kind = str(event.get("id")), str(event.get("type"))
    new = db.execute(
        insert(StripeEvent)
        .values(id=event_id, type=kind[:80])
        .on_conflict_do_nothing(index_elements=["id"])
        .returning(StripeEvent.id)
    ).first()
    if new is None:
        return {"received": True, "duplicate": True}
    obj = (event.get("data") or {}).get("object") or {}
    if not kind.startswith(HANDLED_PREFIXES):
        return {"received": True, "ignored": True}
    customer = _customer_id(obj)
    row = db.scalar(select(OrgBilling).where(OrgBilling.stripe_customer_id == customer)) if customer else None
    if row is None:
        org_id = _org_hint(obj)
        if org_id is not None and db.get(Organization, org_id) is not None:
            row = billing_row(db, org_id)
            if customer and not row.stripe_customer_id:
                row.stripe_customer_id = customer
    stored = db.get(StripeEvent, event_id)
    if row is None:
        log.warning("Stripe event %s (%s): no organization for customer %s", event_id, kind, customer)
        if stored is not None:
            stored.error = f"no organization for customer {customer}"[:300]
        return {"received": True, "unknown_customer": True}
    if stored is not None:
        stored.org_id = row.org_id
    changed = sync(db, row)
    log.info(
        "Stripe event %s (%s) for org %s: %s",
        event_id,
        kind,
        row.org_id,
        "plan changed" if changed else "no change",
    )
    return {"received": True, "changed": changed}


# --- Stripe setup (products, prices, portal) -------------------------------------------------------


def _portal_features(products: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "tax_id", "name"]},
        "invoice_history": {"enabled": True},
        "payment_method_update": {"enabled": True},
        "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
        "subscription_update": {
            "enabled": True,
            "default_allowed_updates": ["price"],
            "proration_behavior": "create_prorations",
            "products": products,
        },
    }


def setup_stripe() -> list[str]:
    """Create (or update) the CountVision products, prices and portal settings in Stripe.

    Safe to run again. Returns lines for the console."""
    lines: list[str] = []
    portal_products: list[dict[str, Any]] = []
    for plan in PLANS.values():
        assert plan.price_eur is not None
        product_id = f"countvision_{plan.id}"
        found = stripe_api.get("/v1/prices", {"lookup_keys": [plan.lookup_key], "active": True, "limit": 1})
        current = (found.get("data") or [None])[0]
        if (
            current
            and current.get("unit_amount") == plan.price_eur * 100
            and current.get("currency") == "eur"
        ):
            lines.append(f"{plan.name}: price {current['id']} ({plan.price_eur} EUR/month) already there")
            portal_products.append(
                {"product": current.get("product") or product_id, "prices": [current["id"]]}
            )
            continue
        try:
            stripe_api.post(
                "/v1/products",
                {
                    "id": product_id,
                    "name": f"CountVision {plan.name}",
                    "description": plan.blurb,
                    "metadata": {"plan": plan.id},
                },
                idempotency_key=f"countvision-product-{plan.id}",
            )
        except stripe_api.StripeError as exc:
            if "already exists" not in exc.message:
                raise
        price = stripe_api.post(
            "/v1/prices",
            {
                "product": product_id,
                "currency": "eur",
                "unit_amount": plan.price_eur * 100,
                "recurring": {"interval": "month"},
                "lookup_key": plan.lookup_key,
                "transfer_lookup_key": True,
                "nickname": f"{plan.name} monthly",
                "tax_behavior": "exclusive",
                "metadata": {"plan": plan.id},
            },
        )
        lines.append(f"{plan.name}: created price {price['id']} ({plan.price_eur} EUR/month)")
        portal_products.append({"product": product_id, "prices": [price["id"]]})

    params = {
        "business_profile": {"headline": "CountVision: change your plan, card or address"},
        "features": _portal_features(portal_products),
        "metadata": {"app": "countvision"},
    }
    get_redis().delete(PORTAL_CACHE_KEY)
    existing = _portal_config_id()
    if existing:
        stripe_api.post(f"/v1/billing_portal/configurations/{existing}", params)
        lines.append(f"Customer portal: updated settings {existing}")
    else:
        cfg = stripe_api.post("/v1/billing_portal/configurations", params)
        lines.append(f"Customer portal: created settings {cfg['id']}")
    get_redis().delete(PORTAL_CACHE_KEY)
    return lines


# --- operator actions (admin page) ------------------------------------------------------------------


def extend_trial(db: Session, org: Organization, actor: User, days: int) -> OrgBilling:
    if not 1 <= days <= 365:
        raise AppError("Choose 1 to 365 days.", code="invalid", fields={"days": "1 to 365"})
    row = billing_row(db, org.id, lock=True)
    old = row.trial_ends_at
    row.trial_ends_at = max(row.trial_ends_at, utcnow()) + timedelta(days=days)
    audit.record(
        db,
        "admin.trial_extended",
        actor=actor.id,
        org_id=org.id,
        target_type="org",
        target_id=org.id,
        days=days,
        old=_iso(old),
        new=_iso(row.trial_ends_at),
    )
    plan_changed(db, org.id)
    return row


def grant_plan(
    db: Session, org: Organization, actor: User, plan_id: str | None, until: datetime | None, note: str | None
) -> OrgBilling:
    """Give a plan for free until a date (pilot), or remove the grant (plan_id None)."""
    row = billing_row(db, org.id, lock=True)
    if plan_id is None:
        old = row.comp_plan
        row.comp_plan = row.comp_until = row.comp_note = None
        audit.record(
            db,
            "admin.grant_removed",
            actor=actor.id,
            org_id=org.id,
            target_type="org",
            target_id=org.id,
            plan=old,
        )
    else:
        if get_plan(plan_id) is None:
            raise AppError("Unknown plan.", code="unknown_plan", fields={"plan": "Unknown plan"})
        if until is None or until <= utcnow():
            raise AppError(
                "Choose an end date in the future.", code="invalid", fields={"until": "In the past"}
            )
        row.comp_plan, row.comp_until, row.comp_note = plan_id, until, (note or "").strip()[:200] or None
        audit.record(
            db,
            "admin.plan_granted",
            actor=actor.id,
            org_id=org.id,
            target_type="org",
            target_id=org.id,
            plan=plan_id,
            until=_iso(until),
            note=row.comp_note,
        )
    plan_changed(db, org.id)
    return row
