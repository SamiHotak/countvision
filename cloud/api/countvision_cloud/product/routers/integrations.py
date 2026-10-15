"""/api/orgs/{org_id}/... alerts, notification channels, API keys and webhook deliveries.

Roles: everybody in the organization sees alerts and their history. Members create and change
alert rules and channels (and send test messages). Admins manage API keys and see webhook secrets.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...db import get_db, utcnow
from ...saas.deps import OrgContext, org_access, require_plan
from ...saas.errors import AppError
from ...saas.models import Membership, Role, User
from ...saas.schemas import OkOut
from ...settings import get_settings
from ..models import AlertRule, WebhookDelivery
from ..schemas import AlertRuleIn, AlertRulePatch, ApiKeyIn, ChannelIn, ChannelPatch, TelegramChatsIn
from ..services import alerts, api_keys, channels, notify, sites

router = APIRouter(prefix="/api/orgs/{org_id}", tags=["alerts and integrations"],
                   dependencies=[Depends(require_plan)])  # 402 without a working plan


def _members(db: Session, org_id: uuid.UUID) -> dict[str, User]:
    rows = db.scalars(
        select(User).join(Membership, Membership.user_id == User.id).where(Membership.org_id == org_id)
    )
    return {str(u.id): u for u in rows}


# --- channels -------------------------------------------------------------------------------


@router.get("/channels")
def list_channels(
    ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    members = _members(db, ctx.org.id)
    return [channels.channel_out(db, c, members) for c in channels.list_channels(db, ctx.org)]


@router.post("/channels", status_code=201)
def create_channel(
    body: ChannelIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    ch, secret = channels.create_channel(db, ctx.org, ctx.user, body.model_dump())
    db.commit()
    return {**channels.channel_out(db, ch, _members(db, ctx.org.id)), "signing_secret": secret}


@router.patch("/channels/{channel_id}")
def update_channel(
    channel_id: uuid.UUID,
    body: ChannelPatch,
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    ch = channels.get_channel(db, ctx.org, channel_id)
    secret = channels.update_channel(db, ctx.org, ctx.user, ch, body.model_dump(exclude_unset=True))
    db.commit()
    return {**channels.channel_out(db, ch, _members(db, ctx.org.id)), "signing_secret": secret}


@router.delete("/channels/{channel_id}", response_model=OkOut)
def delete_channel(
    channel_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    channels.delete_channel(db, ctx.org, ctx.user, channels.get_channel(db, ctx.org, channel_id))
    db.commit()
    return OkOut(message="Channel deleted.")


@router.post("/channels/{channel_id}/test", response_model=OkOut)
def test_channel(
    channel_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    ch = channels.get_channel(db, ctx.org, channel_id)
    text = channels.test_channel(db, ctx.org, ctx.user, ch)
    db.commit()
    return OkOut(message=text)


@router.get("/channels/{channel_id}/secret")
def channel_secret(
    channel_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> dict[str, str]:
    return {"signing_secret": channels.reveal_secret(channels.get_channel(db, ctx.org, channel_id))}


@router.post("/channels/telegram/chats")
def telegram_chats(
    body: TelegramChatsIn, ctx: OrgContext = Depends(org_access(Role.MEMBER))
) -> dict[str, Any]:
    """Chats that recently wrote to the bot: the person sends any message to the bot, then picks it."""
    if get_settings().is_local:
        raise AppError("Telegram needs the internet, so CountVision Local does not offer it.",
                       code="not_in_local", status=422)
    return notify.telegram_chats(body.bot_token.strip())


@router.get("/webhook-deliveries")
def deliveries(
    channel_id: uuid.UUID | None = None,
    limit: int = Query(50, ge=1, le=200),
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.org_id == ctx.org.id)
    if channel_id:
        stmt = stmt.where(WebhookDelivery.channel_id == channel_id)
    rows = db.scalars(
        stmt.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id.desc()).limit(limit)
    )
    return [
        {
            "id": r.id,
            "channel_id": r.channel_id,
            "event_id": r.event_id,
            "event_type": r.event_type,
            "status": r.status,
            "attempts": r.attempts,
            "response_code": r.response_code,
            "error": r.error,
            "created_at": r.created_at,
            "delivered_at": r.delivered_at,
            "next_try_at": r.next_try_at,
        }
        for r in rows
    ]


@router.post("/webhook-deliveries/{delivery_id}/retry", response_model=OkOut)
def retry_delivery(
    delivery_id: int, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    row = db.scalar(
        select(WebhookDelivery).where(WebhookDelivery.org_id == ctx.org.id, WebhookDelivery.id == delivery_id)
    )
    if row is None or row.event_type == "test":
        from ...saas.errors import NotFound

        raise NotFound("Delivery not found.")
    ok = notify.attempt(db, row)
    db.commit()
    return OkOut(ok=ok, message="Delivered." if ok else f"Failed again: {row.error}")


# --- alert rules -------------------------------------------------------------------------------


def _rule_out(r: AlertRule) -> dict[str, Any]:
    return {
        "id": r.id,
        "site_id": r.site_id,
        "site_name": r.site.name,
        "name": r.name,
        "kind": r.kind,
        "camera_id": r.camera_id,
        "zone": r.zone,
        "threshold": r.threshold,
        "duration_min": r.duration_min,
        "channel_ids": r.channel_ids,
        "enabled": r.enabled,
        "state": r.state,
        "fired_at": r.fired_at,
        "last_value": r.last_value,
        "last_eval_at": r.last_eval_at,
        "last_message": r.last_message,
        "pending_since": r.true_since if r.state != "firing" else None,
        "created_at": r.created_at,
    }


@router.get("/alert-rules")
def list_rules(
    ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)
) -> list[dict[str, Any]]:
    rows = db.scalars(select(AlertRule).where(AlertRule.org_id == ctx.org.id).order_by(AlertRule.created_at))
    return [_rule_out(r) for r in rows]


@router.post("/alert-rules", status_code=201)
def create_rule(
    body: AlertRuleIn, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> dict[str, Any]:
    site = sites.get_site(db, ctx.org, body.site_id)
    rule = alerts.create_rule(db, ctx.org, ctx.user, site, body.model_dump())
    db.commit()
    return _rule_out(rule)


@router.patch("/alert-rules/{rule_id}")
def update_rule(
    rule_id: uuid.UUID,
    body: AlertRulePatch,
    ctx: OrgContext = Depends(org_access(Role.MEMBER)),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    rule = alerts.get_rule(db, ctx.org, rule_id)
    site = sites.get_site(db, ctx.org, body.site_id) if body.site_id else None
    alerts.update_rule(db, ctx.org, ctx.user, rule, body.model_dump(exclude_unset=True), site)
    db.commit()
    return _rule_out(rule)


@router.delete("/alert-rules/{rule_id}", response_model=OkOut)
def delete_rule(
    rule_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.MEMBER)), db: Session = Depends(get_db)
) -> OkOut:
    alerts.delete_rule(db, ctx.org, ctx.user, alerts.get_rule(db, ctx.org, rule_id))
    db.commit()
    return OkOut(message="Alert deleted.")


@router.post("/alert-rules/{rule_id}/check")
def check_rule(
    rule_id: uuid.UUID, ctx: OrgContext = Depends(org_access()), db: Session = Depends(get_db)
) -> dict:
    """What would the rule say right now? (no state change, nothing sent)"""
    rule = alerts.get_rule(db, ctx.org, rule_id)
    chk = alerts.check(db, rule, utcnow())
    db.rollback()
    return {"true": chk.true, "value": chk.value, "message": chk.text}


@router.get("/alert-events")
def alert_events(
    limit: int = Query(100, ge=1, le=500),
    since: datetime | None = None,
    ctx: OrgContext = Depends(org_access()),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    rows = alerts.history(db, ctx.org, limit, since or (utcnow() - timedelta(days=90)))
    return [
        {
            "id": e.id,
            "rule_id": e.rule_id,
            "site_id": e.site_id,
            "rule_name": e.rule_name,
            "kind": e.kind,
            "status": e.status,
            "value": e.value,
            "message": e.message,
            "created_at": e.created_at,
        }
        for e in rows
    ]


# --- API keys -------------------------------------------------------------------------------------


def _key_out(k) -> dict[str, Any]:
    return {
        "id": k.id,
        "name": k.name,
        "prefix": k.prefix,
        "created_at": k.created_at,
        "last_used_at": k.last_used_at,
        "revoked": k.revoked_at is not None,
    }


@router.get("/api-keys")
def list_keys(ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)) -> list[dict]:
    return [_key_out(k) for k in api_keys.list_keys(db, ctx.org)]


@router.post("/api-keys", status_code=201)
def create_key(
    body: ApiKeyIn, ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> dict:
    row, key = api_keys.create_key(db, ctx.org, ctx.user, body.name)
    db.commit()
    return {**_key_out(row), "key": key}


@router.delete("/api-keys/{key_id}", response_model=OkOut)
def revoke_key(
    key_id: uuid.UUID, ctx: OrgContext = Depends(org_access(Role.ADMIN)), db: Session = Depends(get_db)
) -> OkOut:
    api_keys.revoke_key(db, ctx.org, ctx.user, key_id)
    db.commit()
    return OkOut(message="Key revoked. Programs using it stop working now.")
