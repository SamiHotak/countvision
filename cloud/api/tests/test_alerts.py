"""Phase 4 B: channels (email, Telegram, webhook), alert rules, webhook events, API keys, public API."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import last_link, signup
from fakes import GOOD_TOKEN, FakeTelegram, WebhookReceiver
from sqlalchemy import select, update
from test_dashboards import cov, cross, lc
from test_devices import batch, setup
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import AlertEvent, AlertRule, Device, WebhookDelivery
from countvision_cloud.product.services import alerts, notify, webhook_events
from countvision_cloud.saas.models import Role
from countvision_cloud.saas.services import email
from countvision_cloud.settings import get_settings


@pytest.fixture
def telegram():
    fake = FakeTelegram()
    settings = get_settings()
    old = settings.telegram_api_url
    settings.telegram_api_url = fake.url
    yield fake
    settings.telegram_api_url = old
    fake.close()


@pytest.fixture
def hook():
    rx = WebhookReceiver()
    yield rx
    rx.close()


def verified_setup(client, app):
    org, site, code, dev, body = setup(client, app)
    client.post("/api/auth/verify-email", json={"token": last_link("owner@example.com", "verify-email")})
    me = client.get(f"/api/orgs/{org['id']}/members").json()[0]
    return org, site, dev, body, me


def channel(client, org, **data) -> dict:
    resp = client.post(f"/api/orgs/{org['id']}/channels", json=data)
    assert resp.status_code == 201, resp.text
    return resp.json()


def minute_ts(back: int) -> int:
    now = int(time.time())
    return now - now % 60 - 60 * back


def zone_rows(occ: list[float], kind_col: str = "occ_avg", zone: str | None = None) -> list[dict]:
    """Zone minutes ending with the newest finished minute (occ[-1] = newest)."""
    zone = zone or ("queue" if kind_col == "queue_avg" else "floor")
    rows = []
    for i, v in enumerate(occ):
        back = len(occ) - i
        rows.append({"camera_id": "door", "window_start": minute_ts(back), "zone": zone, "class_name": "*",
                     "sample_s": 60, "occ_avg": v if kind_col == "occ_avg" else 0, "occ_max": int(v) + 1,
                     "occ_last": int(v), "queue_avg": v if kind_col == "queue_avg" else 0, "queue_max": int(v) + 1,
                     "visits": 0, "dwell_sum_s": 0, "dwell_max_s": 0})
    return rows


def area_camera(dev, batch_id="cfg"):
    """The camera reports a config with an AREA zone 'floor' and a QUEUE zone 'queue'."""
    cam = {"id": "door", "name": "Entrance", "state": "running", "connected": True,
           "config": {"lines": [{"name": "entrance", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}],
                      "zones": [{"name": "floor", "polygon": [[0, 0], [1, 0], [1, 1]], "kind": "area"},
                                {"name": "queue", "polygon": [[0, 0], [0.5, 0], [0.5, 0.5]], "kind": "queue"}],
                      "classes": ["person"]}}
    assert dev.post("/api/device/ingest", json=batch(batch_id, cameras=[cam])).status_code == 200


# --------------------------------------------------------------------------- channels

def test_channels_email_telegram_webhook_and_tests(app, client, client2, telegram, hook):
    org, site, dev, body, me = verified_setup(client, app)
    base = f"/api/orgs/{org['id']}/channels"

    # email: only members
    bad = client.post(base, json={"kind": "email", "name": "Team", "recipients": ["00000000-0000-0000-0000-000000000009"]})
    assert bad.status_code == 422 and bad.json()["error"]["code"] == "bad_recipient"
    mail = channel(client, org, kind="email", name="Team", recipients=[me["user_id"]])
    assert mail["recipients"][0]["email"] == "owner@example.com"
    email.SENT.clear()
    assert client.post(f"{base}/{mail['id']}/test").json()["message"].startswith("Test email sent to 1")
    assert email.SENT[-1].subject == "✅ CountVision test message"

    # telegram: find the chat with the token, then save; a wrong token gives Telegram's reason
    wrong = client.post(f"{base}/telegram/chats", json={"bot_token": "000:wrong-token-wrong-token"})
    assert wrong.status_code == 422 and "Unauthorized" in wrong.json()["error"]["message"]
    chats = client.post(f"{base}/telegram/chats", json={"bot_token": GOOD_TOKEN}).json()["chats"]
    assert {c["title"] for c in chats} == {"Ezat", "Shop team"} and chats[0]["bot"] == "cv_test_bot"
    tg = channel(client, org, kind="telegram", name="My phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    assert tg["chat_title"] == "Ezat" and tg["secret_ok"] is True and "bot_token" not in tg
    assert client.post(f"{base}/{tg['id']}/test").status_code == 200
    assert telegram.messages[-1]["chat_id"] == 4242 and "test message" in telegram.messages[-1]["text"]
    lost = channel(client, org, kind="telegram", name="Old group", bot_token=GOOD_TOKEN, chat_id=999, chat_title="x")
    err = client.post(f"{base}/{lost['id']}/test")
    assert err.status_code == 422 and "chat not found" in err.json()["error"]["message"]
    assert client.get(base).json()[2]["last_error"].startswith("Telegram says")

    # webhook: the secret is shown once, admins can show it again, every request is signed
    wh = channel(client, org, kind="webhook", name="My system", url=hook.url + "/in", events=["crossing"])
    secret = wh["signing_secret"]
    assert secret.startswith("whsec_")
    assert "signing_secret" not in client.get(base).json()[3]
    assert client.get(f"{base}/{wh['id']}/secret").json()["signing_secret"] == secret
    assert client.post(f"{base}/{wh['id']}/test").json()["message"] == "Webhook answered 200."
    req = hook.requests[-1]
    assert req["json"]["type"] == "test" and req["headers"]["x-countvision-event"] == "test"
    assert notify.verify(secret, req["body"], req["headers"]["x-countvision-signature"])
    assert not notify.verify("whsec_other", req["body"], req["headers"]["x-countvision-signature"])
    hook.codes.append(500)
    failed = client.post(f"{base}/{wh['id']}/test")
    assert failed.status_code == 422 and "HTTP 500" in failed.json()["error"]["message"]
    assert client.post(base, json={"kind": "webhook", "name": "x", "url": "ftp://x"}).status_code == 422

    # viewers see channels but cannot change them or see secrets
    signup(client2, "viewer@example.com", "Vera")
    add_member(org["id"], "viewer@example.com", Role.VIEWER)
    assert client2.get(base).status_code == 200
    assert client2.post(f"{base}/{tg['id']}/test").status_code == 403
    assert client2.get(f"{base}/{wh['id']}/secret").status_code == 403
    actions = [e["action"] for e in client.get(f"/api/orgs/{org['id']}/audit").json()]
    assert "channel.created" in actions and "channel.tested" in actions


def test_private_webhook_targets_are_refused_when_not_allowed(app, client, hook, monkeypatch):
    org, site, dev, body, me = verified_setup(client, app)
    wh = channel(client, org, kind="webhook", name="Local", url=hook.url)
    monkeypatch.setattr(get_settings(), "webhook_allow_private", False)
    resp = client.post(f"/api/orgs/{org['id']}/channels/{wh['id']}/test")
    assert resp.status_code == 422 and "private network" in resp.json()["error"]["message"]
    assert hook.requests == []


# --------------------------------------------------------------------------- alert rules

def rule(client, org, site, **data) -> dict:
    resp = client.post(f"/api/orgs/{org['id']}/alert-rules", json={"site_id": site["id"], **data})
    assert resp.status_code == 201, resp.text
    return resp.json()


def evaluate(now=None) -> dict:
    with session_factory()() as db:
        return alerts.evaluate_all(db, now or utcnow())


def test_occupancy_alert_fires_once_and_resolves(app, client, telegram, hook):
    org, site, dev, body, me = verified_setup(client, app)
    area_camera(dev)
    tg = channel(client, org, kind="telegram", name="Phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    wh = channel(client, org, kind="webhook", name="Sys", url=hook.url)
    mail = channel(client, org, kind="email", name="Me", recipients=[me["user_id"]])
    assert client.post(f"/api/orgs/{org['id']}/alert-rules",
                       json={"site_id": site["id"], "kind": "occupancy", "threshold": 20, "channel_ids": []}
                       ).status_code == 422
    r = rule(client, org, site, kind="occupancy", name="Shop full", zone="floor", threshold=20, duration_min=3,
             channel_ids=[tg["id"], wh["id"], mail["id"]])

    # only 2 minutes above the limit: not yet
    dev.post("/api/device/ingest", json=batch("z1", zone_stats=zone_rows([10, 25, 26])[1:]))
    assert evaluate()["fired"] == 0
    # 3 minutes above: fire, once
    email.SENT.clear()
    dev.post("/api/device/ingest", json=batch("z2", zone_stats=zone_rows([24, 25, 26])))
    assert evaluate()["fired"] == 1
    assert evaluate()["fired"] == 0
    assert "Shop full" in telegram.messages[-1]["text"] and "26 people inside in floor" in telegram.messages[-1]["text"]
    fired = hook.of_type("alert.fired")[0]["json"]["data"]
    assert fired["value"] == 26 and fired["status"] == "fired" and fired["site_name"] == "Shop Mitte"
    assert any("Shop full" in m.subject for m in email.SENT)
    listed = client.get(f"/api/orgs/{org['id']}/alert-rules").json()[0]
    assert listed["state"] == "firing" and listed["fired_at"]

    # one minute below is not enough to end it (no flapping), two minutes are
    dev.post("/api/device/ingest", json=batch("z3", zone_stats=zone_rows([25, 26, 12])))
    assert evaluate()["resolved"] == 0
    dev.post("/api/device/ingest", json=batch("z4", zone_stats=zone_rows([26, 11, 12])))
    assert evaluate()["resolved"] == 1
    assert "is over" in telegram.messages[-1]["text"]
    assert len(hook.of_type("alert.resolved")) == 1
    events = client.get(f"/api/orgs/{org['id']}/alert-events").json()
    assert [e["status"] for e in events] == ["resolved", "fired"]
    assert client.post(f"/api/orgs/{org['id']}/alert-rules/{r['id']}/check").json()["true"] is False


def test_queue_alert_and_stale_data_keeps_state(app, client, telegram):
    org, site, dev, body, me = verified_setup(client, app)
    area_camera(dev)
    tg = channel(client, org, kind="telegram", name="Phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    rule(client, org, site, kind="queue", threshold=4, duration_min=2, channel_ids=[tg["id"]])
    dev.post("/api/device/ingest", json=batch("q", zone_stats=zone_rows([5, 6], "queue_avg")))
    assert evaluate()["fired"] == 1 and "6 people waiting" in telegram.messages[-1]["text"]
    # 10 minutes later without new numbers: nothing is decided (the camera rule is for that)
    assert evaluate(utcnow() + timedelta(minutes=10)) == {"checked": 1, "fired": 0, "resolved": 0, "errors": 0}


def test_camera_offline_alert_and_counting_hours(app, client, telegram):
    org, site, dev, body, me = verified_setup(client, app)
    area_camera(dev)
    tg = channel(client, org, kind="telegram", name="Phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    r = rule(client, org, site, kind="camera_offline", duration_min=5, channel_ids=[tg["id"]])
    assert evaluate()["fired"] == 0  # online
    with session_factory()() as db:
        db.execute(update(Device).values(last_seen_at=utcnow() - timedelta(minutes=3)))
        db.commit()
    assert evaluate()["fired"] == 0  # offline for 3 min only
    with session_factory()() as db:
        db.execute(update(Device).values(last_seen_at=utcnow() - timedelta(minutes=6)))
        db.commit()
    assert evaluate()["fired"] == 1
    assert "Entrance (computer Mini PC 1 offline)" in telegram.messages[-1]["text"]
    dev.post("/api/device/ingest", json=batch("back", cameras=[{"id": "door", "state": "running", "connected": True}]))
    assert evaluate()["resolved"] == 1

    # a camera that only counts on another weekday -> not "offline" today
    with session_factory()() as db:
        db.execute(update(Device).values(last_seen_at=utcnow() - timedelta(hours=2)))
        db.commit()
    cam_id = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()["cameras"][0]["id"]
    from countvision_cloud.product.models import Camera

    with session_factory()() as db:
        db.execute(update(Camera).values(reported_config={
            "lines": [], "zones": [], "classes": ["person"], "anchor": "bottom_center",
            "schedule": {"days": [(datetime.now(ZoneInfo("Europe/Berlin")).weekday() + 3) % 7],
                         "start": "08:00", "end": "09:00"}}))
        db.commit()
    chk = client.post(f"/api/orgs/{org['id']}/alert-rules/{r['id']}/check").json()
    assert chk["true"] is False and "No camera has to count" in chk["message"]
    assert cam_id


def test_traffic_drop_alert(app, client, telegram):
    org, site, dev, body, me = verified_setup(client, app)
    tg = channel(client, org, kind="telegram", name="Phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    r = rule(client, org, site, kind="traffic_drop", threshold=50, duration_min=60, channel_ids=[tg["id"]])
    end = minute_ts(2)
    start = end - 3600
    lines, coverage = [], []
    for w in range(0, 5):
        s = start - w * 7 * 86400
        coverage += cov("door", s, 60)
        n = 5 if w == 0 else 40  # now 5 visitors, normally 40
        lines += [lc("door", s + 60 * i, "entrance", "*", 1) for i in range(n)]
    resp = dev.post("/api/device/ingest", json=batch("hist", line_counts=lines, coverage=coverage))
    assert resp.json()["rejected"] == 0
    assert evaluate()["fired"] == 1
    assert "5 visitors in the last 60 min, normally 40 (12 %" in telegram.messages[-1]["text"]
    # checked only every 5 minutes
    with session_factory()() as db:
        assert db.get(AlertRule, r["id"]).last_value == 12.5
    assert evaluate(utcnow() + timedelta(seconds=30))["checked"] == 1


def test_rule_validation_roles_and_channel_delete(app, client, client2, telegram):
    org, site, dev, body, me = verified_setup(client, app)
    tg = channel(client, org, kind="telegram", name="Phone", bot_token=GOOD_TOKEN, chat_id=4242, chat_title="Ezat")
    url = f"/api/orgs/{org['id']}/alert-rules"
    assert client.post(url, json={"site_id": site["id"], "kind": "traffic_drop", "threshold": 150,
                                  "channel_ids": [tg["id"]]}).status_code == 422
    r = rule(client, org, site, kind="camera_offline", duration_min=10, channel_ids=[tg["id"]])
    assert r["name"] == "Camera not counting"
    patched = client.patch(f"{url}/{r['id']}", json={"duration_min": 15, "enabled": False}).json()
    assert patched["duration_min"] == 15 and patched["enabled"] is False
    signup(client2, "v@example.com", "Vic")
    add_member(org["id"], "v@example.com", Role.VIEWER)
    assert client2.get(url).status_code == 200
    assert client2.post(url, json={"site_id": site["id"], "kind": "camera_offline",
                                   "channel_ids": [tg["id"]]}).status_code == 403
    client.delete(f"/api/orgs/{org['id']}/channels/{tg['id']}")
    assert client.get(url).json()[0]["channel_ids"] == []
    assert client.delete(f"{url}/{r['id']}").status_code == 200


# --------------------------------------------------------------------------- webhook events

def test_crossing_webhook_once_per_new_crossing(app, client, hook):
    org, site, dev, body, me = verified_setup(client, app)
    channel(client, org, kind="webhook", name="POS", url=hook.url, events=["crossing"])
    now = time.time()
    rows = dict(events=[cross("a", now - 3), cross("b", now - 2, direction="out"), cross("c", now - 1)])
    dev.post("/api/device/ingest", json=batch("x1", **rows))
    dev.post("/api/device/ingest", json=batch("x1", **rows))  # network retry: no second webhook
    got = hook.of_type("crossing")
    assert len(got) == 1
    data = got[0]["json"]["data"]
    assert data["count"] == 3 and [c["direction"] for c in data["crossings"]] == ["in", "out", "in"]
    assert data["crossings"][0]["camera_name"] == "Entrance" and data["site_name"] == "Shop Mitte"


def test_webhook_retries_then_fails(app, client, hook):
    org, site, dev, body, me = verified_setup(client, app)
    channel(client, org, kind="webhook", name="POS", url=hook.url, events=["crossing"])
    hook.codes.extend([503, 500])
    dev.post("/api/device/ingest", json=batch("r1", events=[cross("r", time.time() - 1)]))
    with session_factory()() as db:
        row = db.scalars(select(WebhookDelivery)).one()
        assert (row.status, row.attempts, row.response_code) == ("pending", 1, 503)
        first_retry = row.next_try_at
        assert notify.due_deliveries(db, utcnow()) == []  # not before 1 minute
        assert notify.due_deliveries(db, first_retry) == [row.id]
        assert notify.attempt(db, row, first_retry) is False and row.attempts == 2
        assert notify.attempt(db, row, row.next_try_at) is True and row.status == "ok"
        db.commit()
    assert len(hook.requests) == 3
    assert len({r["headers"]["x-countvision-delivery"] for r in hook.requests}) == 1  # same event id: receiver can dedupe

    hook.codes.extend([500] * 10)
    dev.post("/api/device/ingest", json=batch("r2", events=[cross("s", time.time() - 1)]))
    with session_factory()() as db:
        row = db.scalars(select(WebhookDelivery).where(WebhookDelivery.status == "pending")).one()
        for _ in range(len(notify.RETRY_MINUTES)):
            notify.attempt(db, row, row.next_try_at)
        assert row.status == "failed" and row.attempts == 6
        db.commit()
    log = client.get(f"/api/orgs/{org['id']}/webhook-deliveries").json()
    assert [d["status"] for d in log] == ["failed", "ok"]


def test_hourly_counts_webhook(app, client, hook):
    org, site, dev, body, me = verified_setup(client, app)
    channel(client, org, kind="webhook", name="BI", url=hook.url, events=["counts.hourly"])
    now = utcnow()
    start, end = webhook_events._last_hour("Europe/Berlin", now + timedelta(hours=1))
    dev.post("/api/device/ingest", json=batch("h", line_counts=[
        lc("door", int(start.timestamp()) + 120, "entrance", "*", 7, 2)], coverage=cov("door", int(start.timestamp()), 5)))
    with session_factory()() as db:
        assert webhook_events.send_hourly(db, now) == 0  # first run only remembers the hour
    with session_factory()() as db:
        assert webhook_events.send_hourly(db, now + timedelta(hours=1)) == 1
        assert webhook_events.send_hourly(db, now + timedelta(hours=1, minutes=5)) == 0
    data = hook.of_type("counts.hourly")[0]["json"]["data"]
    assert (data["in"], data["out"]) == (7, 2) and data["lines"][0]["line"] == "entrance"
    assert data["hour_start"] == start.isoformat()


# --------------------------------------------------------------------------- public API

def test_api_keys_and_public_api(app, client, client2):
    org, site, dev, body, me = verified_setup(client, app)
    now = int(time.time())
    m = now - now % 60 - 120
    dev.post("/api/device/ingest", json=batch("p", line_counts=[lc("door", m, "entrance", "*", 3, 1),
                                                                lc("door", m, "entrance", "person", 3, 1)],
                                              coverage=cov("door", m, 1), events=[cross("live1", now - 2)]))
    resp = client.post(f"/api/orgs/{org['id']}/api-keys", json={"name": "Power BI"})
    assert resp.status_code == 201
    key = resp.json()["key"]
    assert key.startswith("cvk_") and len(key) > 40
    assert "key" not in client.get(f"/api/orgs/{org['id']}/api-keys").json()[0]

    from fastapi.testclient import TestClient

    api = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    sites = api.get("/api/v1/sites").json()
    assert sites == [{"id": site["id"], "name": "Shop Mitte", "timezone": "Europe/Berlin", "address": None}]
    live = api.get(f"/api/v1/sites/{site['id']}/live").json()
    if datetime.fromtimestamp(m, ZoneInfo("Europe/Berlin")).date() == datetime.now(ZoneInfo("Europe/Berlin")).date():
        assert live["visitors_in"] == 4  # 3 in finished minutes + 1 newer crossing
    day = datetime.fromtimestamp(m, ZoneInfo("Europe/Berlin")).date().isoformat()
    counts = api.get(f"/api/v1/sites/{site['id']}/counts", params={"from": day, "bucket": "day"}).json()
    assert counts["total_in"] >= 3 and counts["data"][0]["t"].startswith(day)
    by_line = api.get(f"/api/v1/sites/{site['id']}/counts", params={"from": day, "by": "line"}).json()
    assert by_line["data"][0]["line"] == "entrance" and by_line["data"][0]["counts_as_visitors"] is True
    by_class = api.get(f"/api/v1/sites/{site['id']}/counts", params={"from": day, "by": "class"}).json()
    assert {d["class_name"] for d in by_class["data"]} == {"person"}
    assert api.get(f"/api/v1/sites/{site['id']}/zones").status_code == 200
    cams = api.get("/api/v1/cameras").json()
    assert cams[0]["name"] == "Entrance" and cams[0]["online"] is True
    assert api.get("/api/v1/alerts").json() == []
    assert api.get("/api/v1/openapi.json").json()["info"]["title"] == "CountVision public API"

    # other organizations' sites are invisible
    signup(client2, "other@example.com", "Otto")
    other = client2.post("/api/orgs", json={"name": "Other"}).json()
    other_site = client2.post(f"/api/orgs/{other['id']}/sites", json={"name": "Theirs"}).json()
    assert api.get(f"/api/v1/sites/{other_site['id']}/live").status_code == 404

    # no key, wrong key, revoked key
    assert TestClient(app).get("/api/v1/sites").status_code == 401
    assert TestClient(app, headers={"Authorization": "Bearer cvk_wrong"}).get("/api/v1/sites").status_code == 401
    kid = client.get(f"/api/orgs/{org['id']}/api-keys").json()[0]["id"]
    assert client.delete(f"/api/orgs/{org['id']}/api-keys/{kid}").status_code == 200
    assert api.get("/api/v1/sites").status_code == 401
    # members cannot manage keys
    add_member(org["id"], "other@example.com", Role.MEMBER)
    assert client2.post(f"/api/orgs/{org['id']}/api-keys", json={"name": "x"}).status_code == 403


def test_public_api_rate_limit(app, client, monkeypatch):
    org, site, dev, body, me = verified_setup(client, app)
    key = client.post(f"/api/orgs/{org['id']}/api-keys", json={"name": "k"}).json()["key"]
    monkeypatch.setattr(get_settings(), "api_rate_per_minute", 3)
    from fastapi.testclient import TestClient

    api = TestClient(app, headers={"Authorization": f"Bearer {key}"})
    codes = [api.get("/api/v1/sites").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]


def test_alert_history_in_public_api(app, client, telegram):
    org, site, dev, body, me = verified_setup(client, app)
    with session_factory()() as db:
        db.add(AlertEvent(org_id=org["id"], site_id=site["id"], rule_name="Shop full", kind="occupancy",
                          status="fired", value=25, message="25 people inside"))
        db.commit()
    key = client.post(f"/api/orgs/{org['id']}/api-keys", json={"name": "k"}).json()["key"]
    from fastapi.testclient import TestClient

    rows = TestClient(app, headers={"Authorization": f"Bearer {key}"}).get("/api/v1/alerts").json()
    assert rows[0]["rule_name"] == "Shop full" and rows[0]["status"] == "fired"
