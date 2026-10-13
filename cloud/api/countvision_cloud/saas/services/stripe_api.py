"""A very small Stripe REST client (httpx, form-encoded), plus webhook signature checks.

We do not use the official ``stripe`` package: we need only a handful of calls, httpx is already
a dependency, and tests can point CV_STRIPE_API_URL at a fake Stripe (saas/routers/dev_stripe.py).

Docs: https://docs.stripe.com/api . Only TEST keys are accepted unless CV_STRIPE_ALLOW_LIVE=true
(checked in settings.py).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
import uuid
from typing import Any

import httpx

from ...settings import get_settings
from ..errors import AppError

log = logging.getLogger(__name__)

TIMEOUT_S = 20.0
SIGNATURE_TOLERANCE_S = 300  # Stripe's own libraries use 5 minutes


class StripeError(AppError):
    """Stripe refused a call or could not be reached. The message is safe to show to admins."""

    status = 502
    code = "stripe_error"


def _flatten(data: dict[str, Any], prefix: str = "") -> list[tuple[str, str]]:
    """{"a": {"b": [1, 2]}} -> [("a[b][0]", "1"), ("a[b][1]", "2")] (Stripe's form encoding)."""
    out: list[tuple[str, str]] = []
    for key, value in data.items():
        name = f"{prefix}[{key}]" if prefix else str(key)
        if value is None:
            continue
        if isinstance(value, dict):
            out.extend(_flatten(value, name))
        elif isinstance(value, (list, tuple)):
            for i, item in enumerate(value):
                if isinstance(item, dict):
                    out.extend(_flatten(item, f"{name}[{i}]"))
                else:
                    out.append((f"{name}[{i}]", _scalar(item)))
        else:
            out.append((name, _scalar(value)))
    return out


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def call(
    method: str, path: str, params: dict[str, Any] | None = None, *, idempotency_key: str | None = None
) -> dict[str, Any]:
    """One Stripe API call. GET params go into the query string, POST params into the body."""
    settings = get_settings()
    if not settings.stripe_secret_key:
        raise StripeError(
            "Billing is not set up on this server (no Stripe key).", code="billing_off", status=409
        )
    url = settings.stripe_api_url.rstrip("/") + path
    headers = {"Authorization": f"Bearer {settings.stripe_secret_key}"}
    form = _flatten(params or {})
    if method == "POST":
        headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
    try:
        with httpx.Client(timeout=TIMEOUT_S, follow_redirects=False) as client:
            if method == "GET":
                resp = client.get(url, params=form, headers=headers)
            else:
                resp = client.request(method, url, data=dict(form), headers=headers)  # keys are unique
    except httpx.HTTPError as exc:
        log.warning("Stripe %s %s: %s", method, path, exc)
        raise StripeError(
            "Cannot reach Stripe right now. Please try again in a minute.", code="stripe_unreachable"
        ) from exc
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        msg = err.get("message") or f"HTTP {resp.status_code}"
        log.warning("Stripe %s %s -> %s: %s", method, path, resp.status_code, msg)
        raise StripeError(f"Stripe says: {msg}")
    return data


def get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    return call("GET", path, params)


def post(
    path: str, params: dict[str, Any] | None = None, *, idempotency_key: str | None = None
) -> dict[str, Any]:
    return call("POST", path, params, idempotency_key=idempotency_key)


# --------------------------------------------------------------------------- webhook signatures


def sign(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """The Stripe-Signature header for a payload (used by the fake Stripe and by tests)."""
    t = int(time.time()) if timestamp is None else timestamp
    mac = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    return f"t={t},v1={mac}"


def verify(payload: bytes, header: str | None, secret: str, now: float | None = None) -> None:
    """Raise AppError(400) unless the Stripe-Signature header matches the raw request body."""
    if not header:
        raise AppError("Missing Stripe-Signature header.", code="bad_signature")
    parts: dict[str, list[str]] = {}
    for item in header.split(","):
        key, _, value = item.strip().partition("=")
        parts.setdefault(key, []).append(value)
    try:
        t = int(parts["t"][0])
    except (KeyError, ValueError) as exc:
        raise AppError("Bad Stripe-Signature header.", code="bad_signature") from exc
    if abs((now or time.time()) - t) > SIGNATURE_TOLERANCE_S:
        raise AppError("Stripe-Signature is too old (check the server clock).", code="bad_signature")
    expected = hmac.new(secret.encode(), f"{t}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, v) for v in parts.get("v1", [])):
        raise AppError(
            "Stripe-Signature does not match. Is CV_STRIPE_WEBHOOK_SECRET the secret that "
            "'stripe listen' printed?",
            code="bad_signature",
        )
