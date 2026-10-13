"""A FAKE Stripe for tests and offline development. Never mounted in production.

Mounted at /api/dev/stripe only when the dev router is on (development/test with the memory email
backend). Point the API at it with
    CV_STRIPE_SECRET_KEY=sk_test_fake   CV_STRIPE_WEBHOOK_SECRET=whsec_fake
    CV_STRIPE_API_URL=http://127.0.0.1:8001/api/dev/stripe
It implements the few Stripe calls CountVision uses (customers, prices, products, checkout
sessions, subscriptions, portal) and shows simple "Checkout" and "Customer portal" pages with
buttons. Each change creates a signed webhook event, delivers it to /api/billing/stripe-webhook on
the same server, and keeps it for tests (GET /api/dev/stripe/events).
"""

from __future__ import annotations

import html
import json
import logging
import re
import secrets
import threading
import time
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ...settings import get_settings
from ..plans import PLANS
from ..services import stripe_api

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/dev/stripe", tags=["dev"], include_in_schema=False)

_LOCK = threading.RLock()
STATE: dict[str, Any] = {}
DELIVER = {"on": True}  # tests that replay events themselves can switch delivery off


def reset() -> None:
    with _LOCK:
        STATE.clear()
        STATE.update(
            customers={}, products={}, prices={}, sessions={}, subs={}, configs={}, portal={}, events=[]
        )
        for plan in PLANS.values():  # like a Stripe account where stripe-setup already ran
            pid = f"countvision_{plan.id}"
            STATE["products"][pid] = {"id": pid, "object": "product", "name": f"CountVision {plan.name}"}
            price = {
                "id": f"price_fake_{plan.id}",
                "object": "price",
                "product": pid,
                "active": True,
                "currency": "eur",
                "unit_amount": (plan.price_eur or 0) * 100,
                "lookup_key": plan.lookup_key,
                "recurring": {"interval": "month"},
                "metadata": {"plan": plan.id},
            }
            STATE["prices"][price["id"]] = price


reset()


def _id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(8)}"


def _unflatten(pairs: list[tuple[str, str]]) -> dict[str, Any]:
    """Stripe form keys back into objects: "a[b][0]" -> {"a": {"b": ["..."]}}."""
    root: dict[str, Any] = {}
    for key, value in pairs:
        parts = [m.group(1) or m.group(2) for m in re.finditer(r"([^\[\]]+)|\[([^\]]*)\]", key)]
        node = root
        for i, part in enumerate(parts):
            last = i == len(parts) - 1
            if last:
                node[part] = value
            else:
                node = node.setdefault(part, {})
    return _lists(root)


def _lists(node: Any) -> Any:
    if isinstance(node, dict):
        node = {k: _lists(v) for k, v in node.items()}
        if node and all(k.isdigit() for k in node):
            return [node[k] for k in sorted(node, key=int)]
    return node


def _auth(request: Request) -> None:
    if not request.headers.get("authorization", "").startswith("Bearer sk_test_"):
        raise HTTPException(401, detail="Invalid API Key provided")


async def _params(request: Request) -> dict[str, Any]:
    _auth(request)
    if request.method == "GET":
        return _unflatten(list(request.query_params.multi_items()))
    form = await request.form()
    return _unflatten([(k, str(v)) for k, v in form.multi_items()])


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error"}}, status_code=status)


def _emit(request: Request, kind: str, obj: dict[str, Any]) -> None:
    event = {
        "id": _id("evt"),
        "object": "event",
        "type": kind,
        "created": int(time.time()),
        "data": {"object": obj},
    }
    body = json.dumps(event).encode()
    secret = get_settings().stripe_webhook_secret or "whsec_fake"
    sig = stripe_api.sign(body, secret)
    with _LOCK:
        STATE["events"].append({"id": event["id"], "type": kind, "body": body.decode(), "signature": sig})
    if not DELIVER["on"]:
        return
    target = str(request.base_url).rstrip("/") + "/api/billing/stripe-webhook"
    try:
        httpx.post(
            target,
            content=body,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
            timeout=15,
        )
    except httpx.HTTPError as exc:  # tests replay the stored events themselves
        log.info("Fake Stripe: could not deliver %s to %s: %s", kind, target, exc)


# --- API ----------------------------------------------------------------------------------------


@router.post("/v1/customers")
async def create_customer(request: Request) -> dict:
    p = await _params(request)
    cust = {
        "id": _id("cus"),
        "object": "customer",
        "email": p.get("email"),
        "name": p.get("name"),
        "metadata": p.get("metadata", {}),
    }
    with _LOCK:
        STATE["customers"][cust["id"]] = cust
    return cust


@router.get("/v1/prices")
async def list_prices(request: Request) -> dict:
    p = await _params(request)
    keys = p.get("lookup_keys") or []
    keys = keys if isinstance(keys, list) else [keys]
    data = [x for x in STATE["prices"].values() if x["active"] and (not keys or x.get("lookup_key") in keys)]
    return {"object": "list", "data": data}


@router.post("/v1/products")
async def create_product(request: Request):
    p = await _params(request)
    pid = p.get("id") or _id("prod")
    with _LOCK:
        if pid in STATE["products"]:
            return _error(400, f"Product already exists: {pid}")
        STATE["products"][pid] = {"id": pid, "object": "product", "name": p.get("name")}
        return STATE["products"][pid]


@router.post("/v1/prices")
async def create_price(request: Request) -> dict:
    p = await _params(request)
    price = {
        "id": _id("price"),
        "object": "price",
        "product": p.get("product"),
        "active": True,
        "currency": p.get("currency"),
        "unit_amount": int(p.get("unit_amount", 0)),
        "lookup_key": p.get("lookup_key"),
        "recurring": p.get("recurring"),
        "metadata": p.get("metadata", {}),
    }
    with _LOCK:
        if p.get("transfer_lookup_key") == "true":
            for other in STATE["prices"].values():
                if other.get("lookup_key") == price["lookup_key"]:
                    other["lookup_key"] = None
        STATE["prices"][price["id"]] = price
    return price


@router.post("/v1/checkout/sessions")
async def create_session(request: Request):
    p = await _params(request)
    if p.get("customer") not in STATE["customers"]:
        return _error(400, "No such customer")
    price_id = (p.get("line_items") or [{}])[0].get("price")
    if price_id not in STATE["prices"]:
        return _error(400, f"No such price: {price_id}")
    sid = _id("cs_test")
    session = {
        "id": sid,
        "object": "checkout.session",
        "mode": p.get("mode"),
        "customer": p["customer"],
        "client_reference_id": p.get("client_reference_id"),
        "metadata": p.get("metadata", {}),
        "success_url": p.get("success_url"),
        "cancel_url": p.get("cancel_url"),
        "status": "open",
        "price": price_id,
        "subscription_data": p.get("subscription_data", {}),
        "subscription": None,
        "url": str(request.base_url).rstrip("/") + f"/api/dev/stripe/checkout/{sid}",
    }
    with _LOCK:
        STATE["sessions"][sid] = session
    return session


@router.get("/v1/subscriptions")
async def list_subscriptions(request: Request) -> dict:
    p = await _params(request)
    data = [s for s in STATE["subs"].values() if s["customer"] == p.get("customer")]
    if p.get("status", "all") != "all":
        data = [s for s in data if s["status"] == p["status"]]
    return {"object": "list", "data": sorted(data, key=lambda s: s["created"], reverse=True)}


@router.get("/v1/subscriptions/{sub_id}")
async def get_subscription(sub_id: str, request: Request):
    _auth(request)
    sub = STATE["subs"].get(sub_id)
    return sub if sub else _error(404, "No such subscription")


@router.post("/v1/billing_portal/sessions")
async def portal_session(request: Request):
    p = await _params(request)
    if p.get("customer") not in STATE["customers"]:
        return _error(400, "No such customer")
    pid = _id("bps")
    session = {
        "id": pid,
        "object": "billing_portal.session",
        "customer": p["customer"],
        "return_url": p.get("return_url"),
        "configuration": p.get("configuration"),
        "url": str(request.base_url).rstrip("/") + f"/api/dev/stripe/portal/{pid}",
    }
    with _LOCK:
        STATE["portal"][pid] = session
    return session


@router.get("/v1/billing_portal/configurations")
async def list_configs(request: Request) -> dict:
    await _params(request)
    return {"object": "list", "data": list(STATE["configs"].values())}


@router.post("/v1/billing_portal/configurations")
async def create_config(request: Request) -> dict:
    p = await _params(request)
    cfg = {
        "id": _id("bpc"),
        "object": "billing_portal.configuration",
        "active": True,
        "metadata": p.get("metadata", {}),
        "features": p.get("features", {}),
    }
    with _LOCK:
        STATE["configs"][cfg["id"]] = cfg
    return cfg


@router.post("/v1/billing_portal/configurations/{cfg_id}")
async def update_config(cfg_id: str, request: Request):
    p = await _params(request)
    cfg = STATE["configs"].get(cfg_id)
    if cfg is None:
        return _error(404, "No such configuration")
    cfg.update(features=p.get("features", cfg["features"]), metadata=p.get("metadata", cfg["metadata"]))
    return cfg


@router.get("/events")
def events() -> list[dict]:
    """Signed events, oldest first (tests post them to the webhook themselves)."""
    return list(STATE["events"])


# --- pages (what the customer sees at Stripe) -------------------------------------------------------

_STYLE = """<style>
body{font:16px system-ui,sans-serif;background:#f6f8fa;color:#1a1f36;margin:0;
padding:40px 16px}
main{max-width:460px;margin:auto;background:#fff;border-radius:10px;padding:28px;box-shadow:0 2px 12px #0002}
.tag{display:inline-block;background:#fff3cd;color:#7a5a00;border-radius:4px;padding:2px 8px;font-size:13px}
button{display:block;width:100%;margin-top:10px;padding:12px;border:0;border-radius:6px;font-size:16px;cursor:pointer}
.pay{background:#635bff;color:#fff}.alt{background:#e3e8ee;color:#1a1f36}.warn{background:#fde2e1;color:#8a1f17}
h1{font-size:22px;margin:12px 0 4px}p{color:#4f566b}</style>"""


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html><head><meta charset=utf-8><meta name=viewport "
        f"content='width=device-width,initial-scale=1'><title>{title}</title>{_STYLE}</head>"
        f"<body><main><span class=tag>FAKE Stripe - test only, no money</span>{body}</main></body></html>"
    )


def _plan_of(price_id: str | None) -> str:
    price = STATE["prices"].get(price_id or "", {})
    plan = PLANS.get((price.get("metadata") or {}).get("plan", ""))
    return f"{plan.name} - {plan.price_eur} EUR / month" if plan else str(price_id)


@router.get("/checkout/{sid}")
def checkout_page(sid: str) -> HTMLResponse:
    s = STATE["sessions"].get(sid)
    if s is None:
        raise HTTPException(404)
    trial = (s.get("subscription_data") or {}).get("trial_end")
    note = (
        f"<p>Free until {time.strftime('%Y-%m-%d', time.gmtime(int(trial)))}, then paid monthly.</p>"
        if trial
        else "<p>Paid monthly.</p>"
    )
    return _page(
        "Checkout",
        f"<h1>{html.escape(_plan_of(s['price']))}</h1>{note}"
        f"<form method=post action='/api/dev/stripe/checkout/{sid}/pay'>"
        f"<button class=pay data-testid=fake-pay>Pay with test card 4242</button></form>"
        f"<form method=post action='/api/dev/stripe/checkout/{sid}/cancel'>"
        f"<button class=alt data-testid=fake-cancel>Back</button></form>",
    )


def _new_sub(s: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    trial_end = (s.get("subscription_data") or {}).get("trial_end")
    trial_end = int(trial_end) if trial_end else None
    return {
        "id": _id("sub"),
        "object": "subscription",
        "customer": s["customer"],
        "status": "trialing" if trial_end else "active",
        "created": now,
        "cancel_at_period_end": False,
        "cancel_at": None,
        "trial_end": trial_end,
        "metadata": (s.get("subscription_data") or {}).get("metadata", {}),
        "items": {
            "object": "list",
            "data": [
                {
                    "id": _id("si"),
                    "price": STATE["prices"][s["price"]],
                    "quantity": 1,
                    "current_period_end": trial_end or now + 30 * 86400,
                }
            ],
        },
    }


@router.post("/checkout/{sid}/pay")
def checkout_pay(sid: str, request: Request) -> RedirectResponse:
    with _LOCK:
        s = STATE["sessions"].get(sid)
        if s is None or s["status"] != "open":
            raise HTTPException(404)
        sub = _new_sub(s)
        STATE["subs"][sub["id"]] = sub
        s.update(status="complete", subscription=sub["id"])
    _emit(request, "checkout.session.completed", s)
    _emit(request, "customer.subscription.created", sub)
    return RedirectResponse(s["success_url"], status_code=303)


@router.post("/checkout/{sid}/cancel")
def checkout_cancel(sid: str) -> RedirectResponse:
    s = STATE["sessions"].get(sid)
    if s is None:
        raise HTTPException(404)
    return RedirectResponse(s["cancel_url"], status_code=303)


def _customer_sub(customer: str) -> dict[str, Any] | None:
    subs = [x for x in STATE["subs"].values() if x["customer"] == customer and x["status"] != "canceled"]
    return max(subs, key=lambda x: x["created"]) if subs else None


@router.get("/portal/{pid}")
def portal_page(pid: str) -> HTMLResponse:
    p = STATE["portal"].get(pid)
    if p is None:
        raise HTTPException(404)
    sub = _customer_sub(p["customer"])
    if sub is None:
        body = "<h1>No subscription</h1>"
    else:
        current = sub["items"]["data"][0]["price"]
        body = (
            f"<h1>{html.escape(_plan_of(current['id']))}</h1><p>Status: {sub['status']}"
            f"{' (ends at period end)' if sub['cancel_at_period_end'] else ''}</p>"
        )
        for price in STATE["prices"].values():
            if price["lookup_key"] and price["id"] != current["id"]:
                plan_id = price["metadata"].get("plan")
                body += (
                    f"<form method=post action='/api/dev/stripe/portal/{pid}/switch/{price['id']}'>"
                    f"<button class=pay data-testid=fake-switch-{plan_id}>Switch to "
                    f"{html.escape(_plan_of(price['id']))}</button></form>"
                )
        for action, label, cls in (
            ("cancel", "Cancel at period end", "warn"),
            ("resume", "Keep my plan", "alt"),
            ("fail", "Simulate a failed payment", "warn"),
            ("end", "End the subscription now", "warn"),
        ):
            body += (
                f"<form method=post action='/api/dev/stripe/portal/{pid}/{action}'>"
                f"<button class={cls} data-testid=fake-{action}>{label}</button></form>"
            )
    body += (
        f"<form method=get action='{html.escape(p['return_url'] or '/')}'>"
        f"<button class=alt data-testid=fake-return>Return to CountVision</button></form>"
    )
    return _page("Customer portal", body)


@router.post("/portal/{pid}/{action}")
@router.post("/portal/{pid}/{action}/{price_id}")
def portal_action(pid: str, action: str, request: Request, price_id: str | None = None) -> RedirectResponse:
    p = STATE["portal"].get(pid)
    if p is None:
        raise HTTPException(404)
    with _LOCK:
        sub = _customer_sub(p["customer"])
        if sub is None:
            raise HTTPException(409)
        kind = "customer.subscription.updated"
        if action == "switch" and price_id in STATE["prices"]:
            sub["items"]["data"][0]["price"] = STATE["prices"][price_id]
        elif action == "cancel":
            sub["cancel_at_period_end"] = True
        elif action == "resume":
            sub["cancel_at_period_end"] = False
            if sub["status"] == "past_due":
                sub["status"] = "active"
        elif action == "fail":
            sub["status"] = "past_due"
        elif action == "end":
            sub["status"], kind = "canceled", "customer.subscription.deleted"
        else:
            raise HTTPException(404)
    _emit(request, kind, sub)
    if action == "fail":
        _emit(
            request,
            "invoice.payment_failed",
            {"object": "invoice", "customer": sub["customer"], "subscription": sub["id"]},
        )
    return RedirectResponse(f"/api/dev/stripe/portal/{pid}", status_code=303)
