"""Billing (Phase 4 C): trial, plan limits, Stripe Checkout + webhooks + portal (fake Stripe),
operator grants, the admin page, and what stops without a plan."""

from __future__ import annotations

import json
import os
import uuid
from datetime import timedelta

import pytest
from conftest import create_org, signup
from fakes import FakeStripe
from sqlalchemy import func, select, update
from test_devices import batch, make_site, minute, new_code, pair
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import Camera, LineCount, VideoJob
from countvision_cloud.saas.models import AuditLog, OrgBilling, Role, StripeEvent, User
from countvision_cloud.saas.services import billing, stripe_api
from countvision_cloud.settings import Settings, get_settings

WEBHOOK_SECRET = "whsec_test_countvision"


@pytest.fixture
def stripe():
    """Billing ON, pointed at a fake Stripe server."""
    fake = FakeStripe()
    old = {k: os.environ.get(k) for k in ("CV_STRIPE_SECRET_KEY", "CV_STRIPE_WEBHOOK_SECRET", "CV_STRIPE_API_URL")}
    os.environ.update(CV_STRIPE_SECRET_KEY="sk_test_fake", CV_STRIPE_WEBHOOK_SECRET=WEBHOOK_SECRET,
                      CV_STRIPE_API_URL=fake.url)
    get_settings.cache_clear()
    yield fake
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    get_settings.cache_clear()
    fake.close()


def cams_batch(batch_id: str, ids: list[str]) -> dict:
    """An upload with several cameras, one IN crossing per camera in the last full minute."""
    w = minute(1)
    return batch(
        batch_id,
        cameras=[{"id": i, "name": i.title(), "state": "running", "connected": True, "fps": 10.0, "reconnects": 0,
                  "lines": ["door"], "zones": []} for i in ids],
        line_counts=[{"camera_id": i, "window_start": w, "line": "door", "class_name": "person",
                      "in_count": 1, "out_count": 0} for i in ids],
    )


def stored_rows(camera_names: list[str]) -> dict[str, int]:
    with session_factory()() as db:
        rows = db.execute(select(Camera.edge_camera_id, func.count(LineCount.camera_id))
                          .join(LineCount, LineCount.camera_id == Camera.id, isouter=True)
                          .where(Camera.edge_camera_id.in_(camera_names)).group_by(Camera.edge_camera_id))
        return dict(rows.all())


def set_billing(org_id: str, **values) -> None:
    with session_factory()() as db:
        db.execute(update(OrgBilling).where(OrgBilling.org_id == uuid.UUID(org_id)).values(**values))
        db.commit()


def owner_org(client, name: str = "Café Sonne") -> dict:
    signup(client, "owner@example.com", "Olga")
    return create_org(client, name)


# ------------------------------------------------------------------------------------------------

def test_billing_off_means_no_limits(client, app):
    org = owner_org(client)
    info = client.get(f"/api/orgs/{org['id']}/billing").json()
    assert info["enabled"] is False and info["state"] == "off" and info["active"] is True
    assert client.get(f"/api/orgs/{org['id']}").json()["billing"]["state"] == "off"
    site = make_site(client, org["id"])
    dev, _ = pair(app, new_code(client, org["id"], site["id"])["code"])
    assert dev.post("/api/device/ingest", json=cams_batch("b1", ["a", "b", "c", "d", "e"])).status_code == 200
    assert stored_rows(["a", "b", "c", "d", "e"]) == {k: 1 for k in "abcde"}  # nothing paused
    assert client.post(f"/api/orgs/{org['id']}/billing/checkout", json={"plan": "shop"}).json()["error"]["code"] \
        == "billing_off"


def test_live_key_refused_unless_allowed():
    with pytest.raises(ValueError, match="not a TEST key"):
        Settings(stripe_secret_key="sk_live_abc")
    assert Settings(stripe_secret_key="sk_live_abc", stripe_allow_live=True).billing_enabled
    assert Settings(stripe_secret_key="sk_test_abc").stripe_test_mode


def test_trial_and_camera_limit(stripe, client, app):
    org = owner_org(client)
    info = client.get(f"/api/orgs/{org['id']}/billing").json()
    assert info["enabled"] and info["test_mode"] and info["state"] == "trial"
    assert info["plan"]["limits"]["cameras"] == 2 and info["trial_days_left"] == 14
    assert [p["id"] for p in info["plans"]] == ["starter", "shop", "business"]
    brief = client.get(f"/api/orgs/{org['id']}").json()["billing"]
    assert brief == {"enabled": True, "state": "trial", "active": True, "plan": "trial",
                     "plan_name": "Free trial", "trial_days_left": 14, "kind": "stripe"}

    site = make_site(client, org["id"])
    dev, _ = pair(app, new_code(client, org["id"], site["id"])["code"])
    # The edge reports 3 cameras on a 2-camera trial: the newest one is paused, its numbers not stored.
    assert dev.post("/api/device/ingest", json=cams_batch("b1", ["a", "b"])).status_code == 200
    assert dev.post("/api/device/ingest", json=cams_batch("b2", ["a", "b", "c"])).status_code == 200
    assert stored_rows(["a", "b", "c"]) == {"a": 1, "b": 1, "c": 0}
    devices = client.get(f"/api/orgs/{org['id']}/devices").json()
    detail = client.get(f"/api/orgs/{org['id']}/devices/{devices[0]['id']}").json()
    assert {c["edge_camera_id"]: c["plan_paused"] for c in detail["cameras"]} == {"a": False, "b": False, "c": True}
    assert client.get(f"/api/orgs/{org['id']}/billing").json()["usage"]["cameras"] == 3

    # No room for another device
    resp = client.post(f"/api/orgs/{org['id']}/pairing-codes", json={"site_id": site["id"], "device_name": "x"})
    assert resp.status_code == 402 and resp.json()["error"]["code"] == "plan_limit"
    assert "includes 2 cameras and you use 3" in resp.json()["error"]["message"]
    with session_factory()() as db:
        actions = list(db.scalars(select(AuditLog.action).where(AuditLog.action.like("camera.plan%"))))
    assert actions == ["camera.plan_paused"]


def checkout(client, fake: FakeStripe, org_id: str, plan: str) -> str:
    resp = client.post(f"/api/orgs/{org_id}/billing/checkout", json={"plan": plan})
    assert resp.status_code == 200, resp.text
    url = resp.json()["url"]
    assert url.startswith(fake.url + "/checkout/cs_test_")
    return url


def test_checkout_webhooks_and_portal(stripe, client, client2, app):
    org = owner_org(client)
    oid = org["id"]
    site = make_site(client, oid)
    dev, _ = pair(app, new_code(client, oid, site["id"])["code"])
    dev.post("/api/device/ingest", json=cams_batch("b1", ["a", "b", "c"]))  # c paused on the trial

    # members may see the plan but not pay
    signup(client2, "member@example.com")
    add_member(oid, "member@example.com", Role.MEMBER)
    assert client2.get(f"/api/orgs/{oid}/billing").status_code == 200
    assert client2.post(f"/api/orgs/{oid}/billing/checkout", json={"plan": "shop"}).status_code == 403
    assert client.post(f"/api/orgs/{oid}/billing/checkout", json={"plan": "gold"}).status_code == 400

    url = checkout(client, stripe, oid, "shop")
    checkout(client, stripe, oid, "shop")  # changed my mind and came back: same Stripe customer
    assert len(stripe.state["customers"]) == 1
    session = next(iter(s for s in stripe.state["sessions"].values() if s["url"] == url))
    # the rest of the 14-day trial is carried into Stripe: paid from the trial's end
    trial_end = int(session["subscription_data"]["trial_end"])
    assert 13 * 86400 < trial_end - utcnow().timestamp() <= 14 * 86400
    assert session["success_url"].endswith(f"/app/orgs/{oid}/billing?checkout=done")

    assert stripe.page_post(url.replace("/checkout/", "/checkout/") + "/pay") == 303
    answers = stripe.deliver_to(client)
    assert [a.get("changed") for a in answers] == [True, False]  # created once, second event no change
    info = client.get(f"/api/orgs/{oid}/billing").json()
    assert info["state"] == "active" and info["plan"]["id"] == "shop" and info["subscribed_plan"] == "shop"
    assert info["paid_from"] is not None and info["has_customer"]
    # 4 cameras now: c counts again
    dev.post("/api/device/ingest", json=cams_batch("b2", ["a", "b", "c"]))
    with session_factory()() as db:
        assert not any(db.scalars(select(Camera.plan_paused)))
    assert client.post(f"/api/orgs/{oid}/billing/checkout", json={"plan": "business"}).json()["error"]["code"] \
        == "already_subscribed"

    # the same event again changes nothing; a wrong signature is refused
    ev = stripe.state["events"][0]
    again = client.post("/api/billing/stripe-webhook", content=ev["body"], headers={"Stripe-Signature": ev["signature"]})
    assert again.json() == {"received": True, "duplicate": True}
    forged = client.post("/api/billing/stripe-webhook", content=ev["body"].replace("evt_", "evt_x"),
                         headers={"Stripe-Signature": ev["signature"]})
    assert forged.status_code == 400 and forged.json()["error"]["code"] == "bad_signature"
    assert client.post("/api/billing/stripe-webhook", content=b"{}").status_code == 400

    # customer portal: downgrade to Starter -> only the oldest camera keeps counting
    portal = client.post(f"/api/orgs/{oid}/billing/portal")
    assert portal.status_code == 200 and client2.post(f"/api/orgs/{oid}/billing/portal").status_code == 403
    purl = portal.json()["url"]
    assert stripe.page_post(f"{purl}/switch/price_fake_starter") == 303
    stripe.deliver_to(client)
    assert client.get(f"/api/orgs/{oid}/billing").json()["plan"]["id"] == "starter"
    with session_factory()() as db:
        paused = dict(db.execute(select(Camera.edge_camera_id, Camera.plan_paused)).all())
    assert paused == {"a": False, "b": True, "c": True}

    # cancel at period end, then a failed payment: still working (Stripe retries), shown as past_due
    stripe.page_post(f"{purl}/cancel")
    stripe.deliver_to(client)
    assert client.get(f"/api/orgs/{oid}/billing").json()["cancel_at_period_end"] is True
    stripe.page_post(f"{purl}/fail")
    stripe.deliver_to(client)
    info = client.get(f"/api/orgs/{oid}/billing").json()
    assert info["state"] == "past_due" and info["active"]
    assert client.get(f"/api/orgs/{oid}/dashboard").status_code == 200

    # the subscription ends after the trial is over -> no plan -> 402 on product pages
    set_billing(oid, trial_ends_at=utcnow() - timedelta(days=1))
    stripe.page_post(f"{purl}/end")
    stripe.deliver_to(client)
    info = client.get(f"/api/orgs/{oid}/billing").json()
    assert info["state"] == "canceled" and not info["active"]
    resp = client.get(f"/api/orgs/{oid}/dashboard")
    assert resp.status_code == 402 and "subscription has ended" in resp.json()["error"]["message"]

    with session_factory()() as db:
        changes = list(db.scalars(select(AuditLog.meta).where(AuditLog.action == "billing.subscription_changed")
                                  .order_by(AuditLog.id)))
        assert [(c["new_plan"], c["new_status"]) for c in changes] == [
            ("shop", "trialing"), ("starter", "trialing"), ("starter", "trialing"), ("starter", "past_due"),
            ("starter", "canceled")]
        assert db.scalar(select(func.count()).select_from(StripeEvent)) == len(stripe.state["events"])


def test_sync_without_webhooks(stripe, client):
    """`stripe listen` not running: the Billing page's sync reads the plan straight from Stripe."""
    org = owner_org(client)
    oid = org["id"]
    set_billing(oid, trial_ends_at=utcnow() + timedelta(hours=10))  # too short to carry into Stripe
    url = checkout(client, stripe, oid, "business")
    session = next(iter(stripe.state["sessions"].values()))
    assert "trial_end" not in session["subscription_data"]
    stripe.page_post(url + "/pay")
    assert client.get(f"/api/orgs/{oid}/billing").json()["state"] == "trial"  # webhook not delivered
    info = client.post(f"/api/orgs/{oid}/billing/sync").json()
    assert info["state"] == "active" and info["plan"]["id"] == "business" and info["paid_from"] is None
    assert info["plan"]["limits"]["cameras"] == 12


def test_webhook_unknown_customer_and_unhandled(stripe, client):
    body = json.dumps({"id": "evt_1", "type": "customer.subscription.updated",
                       "data": {"object": {"object": "subscription", "customer": "cus_nobody"}}}).encode()
    resp = client.post("/api/billing/stripe-webhook", content=body,
                       headers={"Stripe-Signature": stripe_api.sign(body, WEBHOOK_SECRET)})
    assert resp.json() == {"received": True, "unknown_customer": True}
    body = json.dumps({"id": "evt_2", "type": "charge.refunded", "data": {"object": {}}}).encode()
    resp = client.post("/api/billing/stripe-webhook", content=body,
                       headers={"Stripe-Signature": stripe_api.sign(body, WEBHOOK_SECRET)})
    assert resp.json() == {"received": True, "ignored": True}
    old = stripe_api.sign(body, WEBHOOK_SECRET, timestamp=int(utcnow().timestamp()) - 600)
    assert client.post("/api/billing/stripe-webhook", content=body, headers={"Stripe-Signature": old}).status_code == 400


def test_expired_trial_blocks_product_but_keeps_data(stripe, client, app):
    org = owner_org(client)
    oid = org["id"]
    site = make_site(client, oid)
    dev, _ = pair(app, new_code(client, oid, site["id"])["code"])
    dev.post("/api/device/ingest", json=cams_batch("b1", ["a"]))
    key = client.post(f"/api/orgs/{oid}/api-keys", json={"name": "BI"}).json()["key"]
    set_billing(oid, trial_ends_at=utcnow() - timedelta(minutes=1))

    brief = client.get(f"/api/orgs/{oid}").json()["billing"]
    assert brief["state"] == "expired" and brief["active"] is False
    for path in ("/dashboard", f"/sites/{site['id']}/dashboard", "/alert-rules", "/video-jobs", "/api-keys"):
        resp = client.get(f"/api/orgs/{oid}{path}")
        assert resp.status_code == 402, path
        assert resp.json()["error"]["code"] == "subscription_required"
    assert "free trial has ended" in client.get(f"/api/orgs/{oid}/dashboard").json()["error"]["message"]
    api = client.get("/api/v1/sites", headers={"Authorization": f"Bearer {key}", "X-CountVision": ""})
    assert api.status_code == 402
    # still possible: billing, members, sites/devices list, settings
    for path in ("/billing", "/members", "/sites", "/devices", ""):
        assert client.get(f"/api/orgs/{oid}{path}").status_code == 200, path
    # the device keeps uploading and nothing is lost
    assert dev.post("/api/device/ingest", json=cams_batch("b2", ["a"])).status_code == 200
    assert stored_rows(["a"]) == {"a": 1}  # same minute: newest value wins, still there
    # alerts and reports skip the organization
    with session_factory()() as db:
        assert billing.inactive_org_ids(db) == {uuid.UUID(oid)}


def test_admin_page_grants_and_audit(stripe, client, client2):
    org = owner_org(client)
    oid = org["id"]
    signup(client2, "ezat@example.com", "Ezat")
    assert client2.get("/api/admin/orgs").status_code == 404  # not an admin: the page does not exist
    assert client2.get("/api/me").json()["is_platform_admin"] is False
    from countvision_cloud.cli import main

    assert main(["make-admin", "--email", "EZAT@example.com"]) == 0
    assert client2.get("/api/me").json()["is_platform_admin"] is True

    rows = client2.get("/api/admin/orgs").json()
    assert len(rows) == 2 or len(rows) == 1  # Ezat may have no organization
    row = next(r for r in rows if r["id"] == oid)
    assert row["owner_email"] == "owner@example.com" and row["state"] == "trial" and row["members"] == 1
    assert row["devices"] == 0 and row["usage"] == {"cameras": 0, "video_minutes": 0}
    assert client2.get("/api/admin/orgs", params={"q": "owner@"}).json()[0]["id"] == oid
    assert client2.get("/api/admin/orgs", params={"q": "nothing-like-this"}).json() == []

    # pilot: Business for free until a date
    until = (utcnow() + timedelta(days=30)).isoformat()
    resp = client2.post(f"/api/admin/orgs/{oid}/grant", json={"plan": "business", "until": until,
                                                               "note": "Pilot Café Sonne"})
    assert resp.status_code == 200, resp.text
    info = client.get(f"/api/orgs/{oid}/billing").json()
    assert info["state"] == "comp" and info["plan"]["id"] == "business" and info["comp_note"] == "Pilot Café Sonne"
    bad = client2.post(f"/api/admin/orgs/{oid}/grant", json={"plan": "business", "until": "2020-01-01T00:00:00Z"})
    assert bad.status_code == 400
    assert client2.delete(f"/api/admin/orgs/{oid}/grant").status_code == 200
    assert client.get(f"/api/orgs/{oid}/billing").json()["state"] == "trial"

    set_billing(oid, trial_ends_at=utcnow() - timedelta(days=2))
    assert client2.post(f"/api/admin/orgs/{oid}/trial", json={"days": 7}).status_code == 200
    info = client.get(f"/api/orgs/{oid}/billing").json()
    assert info["state"] == "trial" and info["trial_days_left"] == 7

    detail = client2.get(f"/api/admin/orgs/{oid}").json()
    assert [a["action"] for a in detail["audit"]][:3] == ["admin.trial_extended", "admin.grant_removed",
                                                          "admin.plan_granted"]
    assert detail["members_list"] == [{"email": "owner@example.com", "name": "Olga", "role": "owner"}]
    overview = client2.get("/api/admin/overview").json()
    assert overview["organizations"] >= 1 and overview["states"]["trial"] >= 1 and overview["mrr_eur"] == 0
    feed = client2.get("/api/admin/audit", params={"action": "admin."}).json()
    assert {a["action"] for a in feed} >= {"admin.plan_granted", "admin.trial_extended", "admin.added"}
    assert all(a["action"].startswith("admin.") for a in feed)
    # the customer sees the operator's actions in their own activity log
    assert "admin.plan_granted" in [a["action"] for a in client.get(f"/api/orgs/{oid}/audit").json()]
    with session_factory()() as db:
        assert db.scalar(select(User.is_platform_admin).where(User.email == "ezat@example.com"))


def test_video_minutes_limit(stripe, client):
    org = owner_org(client)
    oid = uuid.UUID(org["id"])
    from countvision_cloud.product.services import plan_limits

    with session_factory()() as db:
        jobs = [VideoJob(org_id=oid, name=f"v{i}.mp4", size_bytes=1, duration_s=d, status="ready")
                for i, d in enumerate((25 * 60, 30 * 60 + 1, 10 * 60))]
        db.add_all(jobs)
        db.flush()
        plan_limits.charge_video(db, jobs[0])  # 25 min of 60
        plan_limits.charge_video(db, jobs[0])  # same month again: not counted twice
        plan_limits.charge_video(db, jobs[1])  # 31 min -> 56 of 60
        assert plan_limits.video_minutes_used(db, oid) == 56
        with pytest.raises(Exception) as exc:
            plan_limits.charge_video(db, jobs[2])  # 10 more would be 66 > 60
        assert getattr(exc.value, "code", "") == "plan_limit"
        db.commit()
    assert client.get(f"/api/orgs/{oid}/billing").json()["usage"]["video_minutes"] == 56
    resp = client.post(f"/api/orgs/{oid}/video-jobs", json={"name": "x.mp4", "size_bytes": 1000})
    assert resp.status_code == 201  # 4 minutes left: an upload may start
    with session_factory()() as db:
        db.execute(update(VideoJob).where(VideoJob.name == "v2.mp4")
                   .values(billed_month=plan_limits.month_key(), billed_minutes=4))
        db.commit()
    resp = client.post(f"/api/orgs/{oid}/video-jobs", json={"name": "y.mp4", "size_bytes": 1000})
    assert resp.status_code == 402 and "minutes of video analysis" in resp.json()["error"]["message"]


def test_stripe_setup_creates_prices_and_portal(stripe):
    for p in list(stripe.state["prices"].values()):  # a fresh Stripe account
        p["active"] = False
    stripe.state["products"].clear()
    lines = billing.setup_stripe()
    assert len([line for line in lines if "created price" in line]) == 3
    assert "Customer portal: created settings" in lines[-1]
    cfg = next(iter(stripe.state["configs"].values()))
    update_feature = cfg["features"]["subscription_update"]
    assert update_feature["enabled"] == "true" and len(update_feature["products"]) == 3
    assert cfg["features"]["subscription_cancel"]["mode"] == "at_period_end"
    again = billing.setup_stripe()
    assert all("already there" in line for line in again[:3]) and "updated settings" in again[-1]
    prices = [p for p in stripe.state["prices"].values() if p["active"] and p["lookup_key"]]
    assert sorted(p["unit_amount"] for p in prices) == [3900, 11900, 29900]
