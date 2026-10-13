"""/api/orgs/{org_id}/billing: plan, trial, usage, Stripe Checkout and the customer portal.
/api/billing/stripe-webhook: events from Stripe (signed; no login, no CSRF header).

Roles: everybody in the organization sees the plan; owners and admins pay and manage it.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from sqlalchemy.orm import Session

from ...db import db_session, get_db
from ..deps import OrgContext, org_access
from ..models import Role
from ..schemas import CheckoutIn, UrlOut
from ..services import billing, ratelimit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/orgs/{org_id}/billing", tags=["billing"])
webhook_router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("")
def get_billing(ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)) -> dict[str, Any]:
    out = billing.summary(db, ctx.org.id)
    db.commit()  # a missing billing row may have been created
    return out


@router.post("/checkout", response_model=UrlOut)
def start_checkout(
    body: CheckoutIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> UrlOut:
    """Returns Stripe's payment page. After paying, Stripe sends the browser back to the Billing page."""
    ratelimit.limit("checkout", str(ctx.org.id), 20, 3600, "Too many attempts. Please wait a while.")
    url = billing.checkout(db, ctx.org, ctx.user, body.plan)
    db.commit()
    return UrlOut(url=url)


@router.post("/portal", response_model=UrlOut)
def open_portal(ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)) -> UrlOut:
    """Stripe's customer portal: change plan, card, billing address, download invoices, cancel."""
    url = billing.portal(db, ctx.org, ctx.user)
    db.commit()
    return UrlOut(url=url)


@router.post("/sync")
def sync_now(
    ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    """Read the plan from Stripe now (after Checkout, in case the webhook is late or not set up)."""
    ratelimit.limit("billing_sync", str(ctx.org.id), 30, 600, "Too many updates. Please wait a few minutes.")
    billing.sync(db, billing.billing_row(db, ctx.org.id, lock=True), actor=ctx.user.id)
    db.commit()
    return billing.summary(db, ctx.org.id)


def _process(payload: bytes, signature: str | None) -> dict[str, Any]:
    with db_session() as db:
        return billing.handle_webhook(db, payload, signature)


@webhook_router.post("/stripe-webhook")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """Stripe calls this. Locally: ``stripe listen --forward-to localhost:8001/api/billing/stripe-webhook``.
    An error answer makes Stripe try again later (up to 3 days)."""
    payload = await request.body()
    return await run_in_threadpool(_process, payload, request.headers.get("stripe-signature"))
