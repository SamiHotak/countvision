"""CountVision Local: setup wizard, built-in device, managed cameras, licence, alerts on screen,
no Redis, no email, background timers, backup/restore."""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import pytest
from conftest import OTHER_KEY, PASSWORD, make_key, run_setup
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from countvision_cloud.db import db_session, session_factory, utcnow
from countvision_cloud.local import background, licence, setup
from countvision_cloud.local.store import AsyncLocalStore, LocalStore, Server
from countvision_cloud.product.models import Camera, Device
from countvision_cloud.saas.models import AppSetting, Role
from countvision_cloud.settings import get_settings

# --- helpers ------------------------------------------------------------------------------------------


def edge_creds() -> dict:
    return json.loads((get_settings().edge_dir / "cloud.json").read_text(encoding="utf-8"))


def device_client(app) -> TestClient:
    """The built-in edge agent, with the token the API wrote into its folder."""
    return TestClient(app, headers={"Authorization": f"Bearer {edge_creds()['token']}"})


def add_camera(client, org_id, device_id, name="Entrance", kind="webcam", address="0"):
    return client.post(f"/api/orgs/{org_id}/devices/{device_id}/cameras",
                       json={"name": name, "kind": kind, "address": address})


def add_member(org_id: str, email_addr: str, role: Role, client: TestClient) -> None:
    """Invite by link (local edition: no email) and accept as a new account."""
    inv = client.post(f"/api/orgs/{org_id}/invites", json={"email": email_addr, "role": role.value}).json()
    token = inv["link"].rsplit("/", 1)[1]
    c = TestClient(client.app, headers={"X-CountVision": "1"})
    r = c.post("/api/auth/signup", json={"email": email_addr, "name": "Vic", "password": PASSWORD,
                                         "invite_token": token})
    assert r.status_code == 201, r.text
    return c


# --- the in-process store -----------------------------------------------------------------------------


def test_store_behaves_like_redis():
    r, rb = LocalStore(decode=True, srv=Server()), None
    rb = LocalStore(decode=False, srv=r.srv)
    assert r.get("x") is None and r.set("x", 5) and r.get("x") == "5" and rb.get("x") == b"5"
    assert r.incr("n") == 1 and r.incr("n") == 2
    assert r.expire("n", 100, nx=True) is True and r.expire("n", 5, nx=True) is False
    assert 99 <= r.ttl("n") <= 100 and r.ttl("missing") == -2
    assert r.set("k", "v", nx=True) and r.set("k", "w", nx=True) is None and r.get("k") == "v"
    r.set("short", "1", ex=0.05)
    time.sleep(0.08)
    assert r.get("short") is None
    pipe = r.pipeline()
    pipe.hset("h", "a", "1")
    pipe.expire("h", 60)
    pipe.set("m", "meta", ex=60)
    assert pipe.execute() == [1, True, True]
    assert r.hgetall("h") == {"a": "1"} and r.hget("h", "a") == "1" and r.hdel("h", "a") == 1
    assert r.hgetall("h") == {} and r.exists("h") == 0
    rb.set("img", b"\xff\xd8jpeg")
    assert rb.get("img") == b"\xff\xd8jpeg"
    assert r.delete("x", "k", "none") == 2
    r.flushdb()
    assert r.get("m") is None and r.ping()


def test_store_pubsub_from_other_threads_wakes_asyncio():
    srv = Server()

    async def main() -> list:
        sub = AsyncLocalStore(srv=srv).pubsub()
        await sub.subscribe("cv:live:1")
        got = []
        sender = threading.Thread(target=lambda: [LocalStore(srv=srv).publish("cv:live:1", json.dumps({"n": i}))
                                                  for i in range(3)])
        started = time.monotonic()
        sender.start()
        while len(got) < 3 and time.monotonic() - started < 2:
            msg = await sub.get_message(ignore_subscribe_messages=True, timeout=0.5)
            if msg:
                got.append(json.loads(msg["data"]))
        assert LocalStore(srv=srv).publish("other", "x") == 0
        await sub.aclose()
        assert LocalStore(srv=srv).publish("cv:live:1", "after close") == 0
        assert await sub.get_message(timeout=0.05) is None
        return got

    assert asyncio.run(main()) == [{"n": 0}, {"n": 1}, {"n": 2}]


# --- edition, first start, built-in device ----------------------------------------------------------------


def test_setup_wizard_creates_everything_once(app, client, client2):
    ed = client.get("/api/edition").json()
    assert ed["edition"] == "local" and ed["needs_setup"] is True and ed["signup"] is False
    assert ed["google"] is False and ed["email"] is False and ed["billing"] == "licence"
    # no public sign-up, no Google, no Stripe in the local edition
    r = client.post("/api/auth/signup", json={"email": "x@example.com", "name": "X", "password": PASSWORD})
    assert r.status_code == 403 and r.json()["error"]["code"] == "signup_closed"
    assert client.get("/api/auth/providers").json()["google"] is False
    assert client.get("/api/auth/google/start", follow_redirects=False).status_code == 404

    bad = client.post("/api/local/setup", json={"name": "O", "email": "o@example.com", "password": "short",
                                                "business": "B", "site_name": "S"})
    assert bad.status_code == 422
    out = run_setup(client)
    assert out["me"]["email_verified"] is True and out["me"]["organizations"][0]["name"] == "Café Sonne"
    me = client.get("/api/me").json()  # logged in by the setup
    assert me["email"] == "owner@example.com"
    assert client.get("/api/edition").json()["needs_setup"] is False
    again = client2.post("/api/local/setup", json={"name": "Evil", "email": "evil@example.com",
                                                   "password": PASSWORD, "business": "Mine", "site_name": "S"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "already_set_up"
    # one business per installation
    r = client.post("/api/orgs", json={"name": "Second"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "local_one_org"

    org_id, device_id = out["org_id"], out["device_id"]
    devices = client.get(f"/api/orgs/{org_id}/devices").json()
    assert len(devices) == 1 and devices[0]["builtin"] and devices[0]["manages_cameras"]
    assert devices[0]["name"] == "This computer" and devices[0]["site_name"] == "Main street"
    # the edge agent's files: its token works, its config is valid for the edge package
    creds = edge_creds()
    assert creds["url"] == "http://127.0.0.1:8001" and creds["device_id"] == device_id
    dev = device_client(app)
    assert dev.get("/api/device/me").json()["name"] == "This computer"
    from countvision_edge.config import load_config

    # the edge reads CV_DATA_DIR etc. too: the launcher must start it WITHOUT the API's CV_ variables
    cfg = load_config(get_settings().edge_dir / "edge.yaml", env={})
    assert cfg.cloud.manage_cameras and cfg.cameras == [] and cfg.detector.type == "blobs"
    assert Path(cfg.data_dir) == get_settings().edge_dir
    # the built-in device cannot be removed
    assert client.post(f"/api/orgs/{org_id}/devices/{device_id}/revoke").json()["error"]["code"] == "builtin_device"
    assert client.delete(f"/api/orgs/{org_id}/devices/{device_id}").status_code == 409
    actions = [e["action"] for e in client.get(f"/api/orgs/{org_id}/audit").json()]
    assert "local.setup_done" in actions and "device.paired" in actions


def test_edge_token_renewed_when_files_are_missing(app, client):
    out = run_setup(client)
    old = edge_creds()["token"]
    with db_session() as db:
        assert setup.ensure_edge_files(db) is False  # all fine: nothing changes
    yaml_file = get_settings().edge_dir / "edge.yaml"
    yaml_file.write_text("# edited by hand\ncameras: []\n")
    with db_session() as db:
        assert setup.ensure_edge_files(db) is False  # the token is fine; the config is written again
    assert "manage_cameras: true" in yaml_file.read_text() and edge_creds()["token"] == old
    (get_settings().edge_dir / "cloud.json").unlink()
    with db_session() as db:
        assert setup.ensure_edge_files(db) is True
    new = edge_creds()["token"]
    assert new != old
    assert TestClient(app, headers={"Authorization": f"Bearer {old}"}).get("/api/device/me").status_code == 401
    assert device_client(app).get("/api/device/me").json()["device_id"] == out["device_id"]


# --- cameras managed in the web app -------------------------------------------------------------------


def test_cameras_added_in_the_web_reach_the_device(app, client, client2):
    out = run_setup(client)
    org_id, device_id = out["org_id"], out["device_id"]
    dev = device_client(app)
    first = dev.get("/api/device/poll?rev=-1&wait=0").json()
    assert first["changed"] and first["managed"] == []

    cam = add_camera(client, org_id, device_id, "Entrance", "webcam", "0")
    assert cam.status_code == 201, cam.text
    assert cam.json()["source_label"] == "Webcam 0" and cam.json()["managed"]
    edge_id = cam.json()["edge_camera_id"]
    poll = dev.get(f"/api/device/poll?rev={first['rev']}&wait=0").json()
    assert poll["changed"] and poll["managed"] == [
        {"id": edge_id, "name": "Entrance", "source": {"kind": "webcam", "uri": "0"}}]

    # an IP camera with a password: stored encrypted, shown without the password
    rtsp = add_camera(client, org_id, device_id, "Till", "rtsp", "rtsp://admin:geheim123@192.168.1.20:554/s1")
    assert rtsp.status_code == 201 and rtsp.json()["source_label"] == "rtsp://***@192.168.1.20:554/s1"
    with db_session() as db:
        row = db.scalar(select(Camera).where(Camera.id == rtsp.json()["id"]))
        assert "geheim123" not in (row.source_secret or "") and "geheim123" not in (row.source_label or "")
    managed = dev.get(f"/api/device/poll?rev={poll['rev']}&wait=0").json()["managed"]
    assert [m["source"]["uri"] for m in managed] == ["0", "rtsp://admin:geheim123@192.168.1.20:554/s1"]
    detail = client.get(f"/api/orgs/{org_id}/devices/{device_id}").json()
    assert [c["name"] for c in detail["cameras"]] == ["Entrance", "Till"]
    assert "geheim123" not in json.dumps(detail)

    # mistakes the user can make
    for kind, address, field in (("webcam", "12", "address"), ("rtsp", "http://x/y", "address"),
                                 ("http", "rtsp://cam", "address"), ("rtsp", "rtsp://", "address")):
        r = add_camera(client, org_id, device_id, "X", kind, address)
        assert r.status_code == 422 and field in r.json()["error"]["fields"], (kind, address, r.text)
    dup = add_camera(client, org_id, device_id, "Again", "webcam", "0")
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "duplicate_source"
    # the free trial has room for 2 cameras
    third = add_camera(client, org_id, device_id, "Back door", "http", "http://192.168.1.30:8080/video")
    assert third.status_code == 402 and third.json()["error"]["code"] == "plan_limit"
    assert "licence" in third.json()["error"]["message"].lower()

    # a viewer cannot add or remove cameras
    viewer = add_member(org_id, "viewer@example.com", Role.VIEWER, client)
    assert add_camera(viewer, org_id, device_id, "V", "webcam", "1").status_code == 403
    assert viewer.delete(f"/api/orgs/{org_id}/cameras/{rtsp.json()['id']}").status_code == 403

    # new password for the IP camera -> the device restarts it with the new address
    r = client.put(f"/api/orgs/{org_id}/cameras/{rtsp.json()['id']}/source",
                   json={"kind": "rtsp", "address": "rtsp://admin:neu456@192.168.1.20:554/s1"})
    assert r.status_code == 200
    rev = dev.get("/api/device/poll?rev=-1&wait=0").json()["rev"]
    assert dev.get("/api/device/poll?rev=-1&wait=0").json()["managed"][1]["source"]["uri"].count("neu456") == 1

    # remove: gone from the device list and the page, the numbers stay
    assert client.delete(f"/api/orgs/{org_id}/cameras/{rtsp.json()['id']}").status_code == 200
    poll = dev.get(f"/api/device/poll?rev={rev}&wait=0").json()
    assert poll["changed"] and [m["name"] for m in poll["managed"]] == ["Entrance"]
    assert [c["name"] for c in client.get(f"/api/orgs/{org_id}/devices/{device_id}").json()["cameras"]] == [
        "Entrance"]
    # now there is room again
    assert add_camera(client, org_id, device_id, "Back door", "http", "http://192.168.1.30:8080/video").status_code == 201
    actions = [e["action"] for e in client.get(f"/api/orgs/{org_id}/audit").json()]
    assert {"camera.added", "camera.removed", "camera.source_changed"} <= set(actions)


def test_long_poll_wakes_when_a_camera_is_added(app, client):
    out = run_setup(client)
    dev = device_client(app)
    rev = dev.get("/api/device/poll?rev=-1&wait=0").json()["rev"]
    result: dict = {}

    def wait() -> None:
        started = time.monotonic()
        result["answer"] = dev.get(f"/api/device/poll?rev={rev}&wait=10").json()
        result["s"] = time.monotonic() - started

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.8)
    add_camera(client, out["org_id"], out["device_id"])
    t.join(12)
    assert result["answer"]["changed"] and len(result["answer"]["managed"]) == 1
    assert result["s"] < 3.0


def test_upload_and_live_stream_without_redis(app, client):
    out = run_setup(client)
    cam = add_camera(client, out["org_id"], out["device_id"]).json()
    dev = device_client(app)
    got: list = []

    def read() -> None:
        with TestClient(app, cookies=client.cookies) as c, \
                c.stream("GET", f"/api/orgs/{out['org_id']}/live?max_s=3") as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    got.append(json.loads(line[6:]))

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(1.0)
    now = time.time()
    batch = {"batch_id": "b1", "agent_version": "0.7.0", "sent_at": now, "status": {"version": "0.7.0"},
             "cameras": [{"id": cam["edge_camera_id"], "name": "Entrance", "state": "running", "connected": True,
                          "fps": 10.0, "lines": ["Door"], "zones": []}],
             "events": [{"event_id": "e1", "camera_id": cam["edge_camera_id"], "kind": "line_cross", "ts": now,
                         "name": "Door", "class_name": "person", "direction": "in"}]}
    assert dev.post("/api/device/ingest", json=batch).status_code == 200
    reader.join(6)
    counts = [m for m in got if m["type"] == "count"]
    assert len(counts) == 1 and counts[0]["line"] == "Door"
    detail = client.get(f"/api/orgs/{out['org_id']}/devices/{out['device_id']}").json()
    assert detail["online"] and detail["cameras"][0]["today"] == {"Door": {"in": 1, "out": 0}}


# --- licence ----------------------------------------------------------------------------------------


def test_licence_trial_key_limits_and_expiry(app, client):
    out = run_setup(client)
    org_id, device_id = out["org_id"], out["device_id"]
    info = client.get(f"/api/orgs/{org_id}/licence").json()
    assert info["state"] == "trial" and info["trial_days_left"] == 14 and info["can_check"]
    assert info["plan"]["limits"] == {"cameras": 2, "video_minutes": -1} and info["licence"] is None
    brief = client.get(f"/api/orgs/{org_id}").json()["billing"]
    assert brief["kind"] == "licence" and brief["state"] == "trial" and brief["enabled"]
    # Stripe's billing page does not exist here
    assert client.get(f"/api/orgs/{org_id}/billing").status_code == 404
    assert client.post(f"/api/orgs/{org_id}/billing/checkout", json={"plan": "shop"}).status_code == 404
    assert client.get("/api/admin/overview").status_code == 404

    # wrong keys
    for key, code in (("hello world, this is not it", "licence_invalid"),
                      (make_key(4, key=OTHER_KEY), "licence_invalid"),
                      (make_key(4)[:-6] + "AAAAAA", "licence_invalid"),
                      (make_key(4, valid_until=date.today() - timedelta(days=1)), "licence_expired")):
        r = client.put(f"/api/orgs/{org_id}/licence", json={"key": key})
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (key[:20], r.text)

    for i in range(2):
        assert add_camera(client, org_id, device_id, f"C{i}", "webcam", str(i)).status_code == 201
    assert add_camera(client, org_id, device_id, "C2", "webcam", "2").status_code == 402
    # a real key, pasted from an email with line breaks
    key = make_key(5, valid_until=date.today() + timedelta(days=365), updates_until=date(2027, 10, 31))
    pasted = key[:40] + "\n  " + key[40:]
    r = client.put(f"/api/orgs/{org_id}/licence", json={"key": pasted})
    assert r.status_code == 200, r.text
    info = r.json()
    assert info["state"] == "active" and info["plan"]["limits"]["cameras"] == 5
    assert info["licence"]["customer"] == "Café Sonne GmbH" and info["licence"]["updates_until"] == "2027-10-31"
    assert add_camera(client, org_id, device_id, "C2", "webcam", "2").status_code == 201

    # a smaller licence: the newest camera is paused and not sent to the device any more
    client.put(f"/api/orgs/{org_id}/licence", json={"key": make_key(2)})
    cams = client.get(f"/api/orgs/{org_id}/devices/{device_id}").json()["cameras"]
    assert [c["plan_paused"] for c in cams] == [False, False, True]
    assert [m["name"] for m in device_client(app).get("/api/device/poll?rev=-1&wait=0").json()["managed"]] == [
        "C0", "C1"]

    # members see the licence page, only admins add keys, only owners remove them
    member = add_member(org_id, "m@example.com", Role.MEMBER, client)
    assert member.get(f"/api/orgs/{org_id}/licence").status_code == 200
    assert member.put(f"/api/orgs/{org_id}/licence", json={"key": make_key(9)}).status_code == 403
    assert member.delete(f"/api/orgs/{org_id}/licence").status_code == 403
    assert client.delete(f"/api/orgs/{org_id}/licence").json()["state"] == "trial"
    actions = [e["action"] for e in client.get(f"/api/orgs/{org_id}/audit").json()]
    assert actions.count("licence.added") == 2 and "licence.removed" in actions


def _age_installation(days: int) -> None:
    with db_session() as db:
        row = db.get(AppSetting, "install")
        row.value = {**row.value, "installed_at": (utcnow() - timedelta(days=days)).isoformat()}


def test_trial_end_gates_the_product_but_keeps_data(app, client):
    out = run_setup(client)
    org_id = out["org_id"]
    cam = add_camera(client, org_id, out["device_id"]).json()
    _age_installation(15)
    info = client.get(f"/api/orgs/{org_id}/licence").json()
    assert info["state"] == "expired" and info["active"] is False
    r = client.get(f"/api/orgs/{org_id}/dashboard")
    assert r.status_code == 402 and r.json()["error"]["code"] == "licence_required"
    assert "Licence page" in r.json()["error"]["message"]
    assert add_camera(client, org_id, out["device_id"], "B", "webcam", "1").status_code == 402
    # numbers still arrive and are stored
    now = time.time()
    ev = [{"event_id": "e1", "camera_id": cam["edge_camera_id"], "kind": "line_cross", "ts": now, "name": "Door",
           "class_name": "person", "direction": "in"}]
    assert device_client(app).post("/api/device/ingest", json={"batch_id": "b", "events": ev}).status_code == 200
    # members, settings and the licence page stay open; a key brings everything back
    assert client.get(f"/api/orgs/{org_id}/members").status_code == 200
    assert client.put(f"/api/orgs/{org_id}/licence", json={"key": make_key(3)}).json()["state"] == "active"
    assert client.get(f"/api/orgs/{org_id}/dashboard").status_code == 200
    # a licence that ended
    client.put(f"/api/orgs/{org_id}/licence", json={"key": make_key(3, valid_until=date.today())})
    with db_session() as db:  # two days later
        db.merge(AppSetting(key="clock", value={"max": (utcnow() + timedelta(days=2)).isoformat()}))
    assert client.get(f"/api/orgs/{org_id}/licence").json()["state"] == "licence_expired"


def test_clock_set_back_does_not_extend_the_trial(client):
    out = run_setup(client)
    _age_installation(13)
    assert client.get(f"/api/orgs/{out['org_id']}/licence").json()["state"] == "trial"
    with db_session() as db:  # the PC saw a time 2 days ahead, then the clock was turned back
        db.merge(AppSetting(key="clock", value={"max": (utcnow() + timedelta(days=2)).isoformat()}))
    assert client.get(f"/api/orgs/{out['org_id']}/licence").json()["state"] == "expired"


def test_licence_key_format_and_cli(capsys, tmp_path, monkeypatch):
    from conftest import PRIVATE_KEY

    from countvision_cloud import cli

    key = make_key(4, valid_until=date(2030, 1, 1))
    assert key.startswith("CVL1-") and "." in key
    lic = licence.parse_key(key)
    assert (lic.cameras, lic.valid_until, lic.customer) == (4, date(2030, 1, 1), "Café Sonne GmbH")
    with pytest.raises(licence.LicenceError):
        licence.parse_key(key, public_text=licence.public_text_of(OTHER_KEY))
    with pytest.raises(ValueError):
        licence.create_key(PRIVATE_KEY, customer="X", cameras=0)
    # keygen writes the PRIVATE key outside the repo and never twice; create signs with it
    module = tmp_path / "licence_key.py"
    module.write_text('PUBLIC_KEY = ""\n')
    from countvision_cloud.local import licence_key

    monkeypatch.setattr(licence_key, "__file__", str(module))
    out_dir = tmp_path / "signing"
    assert cli.main(["licence-keygen", "--out", str(out_dir)]) == 0
    assert (out_dir / "licence_private.pem").is_file()
    public = module.read_text().split('"')[1]
    assert len(public) == 43
    with pytest.raises(SystemExit):
        cli.main(["licence-keygen", "--out", str(out_dir)])
    assert cli.main(["licence-create", "--key", str(out_dir / "licence_private.pem"), "--customer", "Bäckerei Ott",
                     "--cameras", "3", "--valid-until", "2027-12-31", "--out", str(tmp_path / "k.txt")]) == 0
    made = (tmp_path / "k.txt").read_text().strip()
    lic = licence.parse_key(made, public_text=public)
    assert lic.customer == "Bäckerei Ott" and lic.cameras == 3 and lic.valid_until == date(2027, 12, 31)
    capsys.readouterr()
    assert cli.main(["licence-show", key]) == 0
    assert "Café Sonne GmbH" in capsys.readouterr().out


def test_production_ignores_the_public_key_override(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "environment", "production")
    assert licence.public_key_text() == licence.licence_key.PUBLIC_KEY.strip()
    monkeypatch.setattr(s, "environment", "test")
    assert licence.public_key_text() == s.licence_public_key


# --- alerts on screen, no email, invites by link ---------------------------------------------------------


def test_alerts_without_channels_and_cloud_only_channels_refused(app, client):
    out = run_setup(client)
    org_id = out["org_id"]
    site_id = out["site_id"]
    r = client.post(f"/api/orgs/{org_id}/channels", json={"kind": "telegram", "name": "Phone",
                                                          "bot_token": "123456789:AAfake-telegram-token", "chat_id": "1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "not_in_local"
    r = client.post(f"/api/orgs/{org_id}/channels", json={"kind": "email", "name": "Mail", "recipients": []})
    assert r.status_code == 422 and r.json()["error"]["code"] == "email_off"
    # an alert that only shows on screen
    rule = client.post(f"/api/orgs/{org_id}/alert-rules", json={
        "site_id": site_id, "kind": "camera_offline", "duration_min": 1, "channel_ids": []})
    assert rule.status_code == 201, rule.text
    cam = add_camera(client, org_id, out["device_id"]).json()
    with db_session() as db:  # the camera reported once, 10 minutes ago
        c = db.get(Camera, cam["id"])
        c.state, c.connected, c.last_seen_at = "running", True, utcnow() - timedelta(minutes=10)
        d = db.get(Device, out["device_id"])
        d.last_seen_at = utcnow() - timedelta(minutes=10)
    from countvision_cloud.product.services import alerts

    with db_session() as db:
        result = alerts.evaluate_all(db)
    assert result["fired"] == 1, result  # no channel: nothing is sent, the bell shows it
    events = client.get(f"/api/orgs/{org_id}/alert-events").json()
    assert events[0]["status"] == "fired" and events[0]["kind"] == "camera_offline"
    rules = client.get(f"/api/orgs/{org_id}/alert-rules").json()
    assert rules[0]["state"] == "firing"


def test_invites_by_link_and_password_without_email(app, client, capsys):
    out = run_setup(client)
    inv = client.post(f"/api/orgs/{out['org_id']}/invites", json={"email": "tom@example.com", "role": "member"})
    assert inv.status_code == 201 and inv.json()["link"].startswith("http://testserver/invite/")
    assert client.get(f"/api/orgs/{out['org_id']}/invites").json()[0].get("link") is None  # shown once
    r = client.post("/api/auth/forgot-password", json={"email": "owner@example.com"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_off"
    from countvision_cloud import cli

    assert cli.main(["set-password", "--email", "owner@example.com", "--password", "a-brand-new-password"]) == 0
    assert client.get("/api/me").status_code == 401  # logged out everywhere
    c = TestClient(app, headers={"X-CountVision": "1"})
    assert c.post("/api/auth/login", json={"email": "owner@example.com",
                                           "password": "a-brand-new-password"}).status_code == 200


# --- background work without Celery -----------------------------------------------------------------------


def test_scheduler_runs_timers_and_survives_failures():
    calls: list[str] = []

    def boom() -> None:
        calls.append("boom")
        raise RuntimeError("broken task")

    timers = [background.Timer("ok", 0.1, lambda: calls.append("ok"), first_after_s=0),
              background.Timer("boom", 0.1, boom, first_after_s=0)]
    sched = background.Scheduler(timers)
    sched.start()
    time.sleep(0.55)
    sched.stop()
    assert calls.count("ok") >= 3 and calls.count("boom") >= 3
    assert timers[1].failures >= 3 and "broken task" in timers[1].last_error
    assert not sched.is_alive()
    done = background.submit(lambda x: x * 2, 21)
    assert done.result(timeout=2) == 42


def test_default_timers_run_once_against_the_database(client):
    run_setup(client)
    for timer in background.default_timers():
        timer.fn()  # every cloud beat task works without Redis and Celery
    with db_session() as db:
        assert db.get(AppSetting, "clock") is not None
    health = client.get("/api/health").json()
    assert health["status"] == "ok" and health["checks"]["redis"] == "ok" and health["checks"]["worker"] == "ok"


def test_settings_of_the_local_edition(tmp_path, monkeypatch):
    from countvision_cloud.settings import Settings

    for key in ("CV_EMAIL_BACKEND", "CV_EMAIL_DELIVERY", "CV_VIDEO_RUNNER", "CV_ENVIRONMENT"):
        monkeypatch.delenv(key, raising=False)
    s = Settings(edition="local", data_dir=str(tmp_path), environment="production")
    assert s.email_backend == "off" and s.email_delivery == "thread" and s.video_runner == "thread"
    secret = (tmp_path / "secret_key").read_text().strip()
    assert s.secret_key == secret and len(secret) > 40  # created once, reused after
    assert Settings(edition="local", data_dir=str(tmp_path), environment="production").secret_key == secret
    assert Settings(edition="local", data_dir=str(tmp_path), smtp_host="mail.local").email_backend == "smtp"
    assert not Settings(edition="local", data_dir=str(tmp_path), stripe_secret_key="sk_test_x").billing_enabled
    with pytest.raises(ValueError):
        Settings(edition="local", data_dir=str(tmp_path), email_delivery="celery")
    with pytest.raises(ValueError):  # the cloud still needs https in production
        Settings(edition="cloud", environment="production", secret_key="x" * 40, web_url="http://a.b")


# --- backup and restore ------------------------------------------------------------------------------------


needs_pg_tools = pytest.mark.skipif(
    not (shutil.which("pg_dump") or (get_settings().pg_bin_dir and Path(get_settings().pg_bin_dir).is_dir())),
    reason="pg_dump/pg_restore not installed (set CV_PG_BIN_DIR)")


@needs_pg_tools
def test_backup_and_restore(app, client, tmp_path):
    out = run_setup(client)
    org_id = out["org_id"]
    cam = add_camera(client, org_id, out["device_id"], "Entrance", "rtsp", "rtsp://u:p4ss@10.0.0.5/live").json()
    client.put(f"/api/orgs/{org_id}/licence", json={"key": make_key(6)})
    made = client.post(f"/api/orgs/{org_id}/backups", json={})
    assert made.status_code == 201, made.text
    name = made.json()["name"]
    assert name.startswith("CountVision-backup-") and name.endswith(".cvbackup")
    listing = client.get(f"/api/orgs/{org_id}/backups").json()
    assert listing["backups"][0]["name"] == name and listing["backups"][0]["organization"] == "Café Sonne"
    dl = client.get(f"/api/orgs/{org_id}/backups/{name}/download")
    assert dl.status_code == 200 and dl.content[:2] == b"PK"
    # a USB stick folder that is not there
    r = client.post(f"/api/orgs/{org_id}/backups", json={"folder": str(tmp_path / "no-stick")})
    assert r.status_code == 400 and r.json()["error"]["code"] == "folder_missing"
    stick = tmp_path / "stick"
    stick.mkdir()
    assert client.post(f"/api/orgs/{org_id}/backups", json={"folder": str(stick)}).status_code == 201
    assert len(list(stick.glob("*.cvbackup"))) == 1

    # changes after the backup
    client.post(f"/api/orgs/{org_id}/sites", json={"name": "Second shop"})
    client.delete(f"/api/orgs/{org_id}/cameras/{cam['id']}")
    assert len(client.get(f"/api/orgs/{org_id}/sites").json()) == 2
    # only the owner, only with the business name typed
    r = client.post(f"/api/orgs/{org_id}/backups/{name}/restore", json={"confirm": "wrong"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "confirm"
    r = client.post(f"/api/orgs/{org_id}/backups/{name}/restore", json={"confirm": "Café Sonne"})
    assert r.status_code == 200, r.text
    assert r.json()["safety_backup"].startswith("before-restore")
    assert client.get("/api/me").status_code == 401  # log in again
    c = TestClient(app, headers={"X-CountVision": "1"})
    assert c.post("/api/auth/login", json={"email": "owner@example.com", "password": PASSWORD}).status_code == 200
    assert [s["name"] for s in c.get(f"/api/orgs/{org_id}/sites").json()] == ["Main street"]
    assert c.get(f"/api/orgs/{org_id}/licence").json()["licence"]["cameras"] == 6
    # the camera is back, its password readable, and the edge agent's token still works
    managed = device_client(app).get("/api/device/poll?rev=-1&wait=0").json()["managed"]
    assert managed == [{"id": cam["edge_camera_id"], "name": "Entrance",
                        "source": {"kind": "rtsp", "uri": "rtsp://u:p4ss@10.0.0.5/live"}}]
    actions = [e["action"] for e in c.get(f"/api/orgs/{org_id}/audit").json()]
    assert "backup.restored" in actions
    names = [b["name"] for b in c.get(f"/api/orgs/{org_id}/backups").json()["backups"]]
    assert any(n.startswith("before-restore") for n in names)

    # upload a backup file from another PC; junk is refused
    data = (get_settings().backups_path / name).read_bytes()
    up = c.put(f"/api/orgs/{org_id}/backups/upload?name=from-laptop.cvbackup", content=data)
    assert up.status_code == 201 and up.json()["name"] == "from-laptop.cvbackup"
    junk = c.put(f"/api/orgs/{org_id}/backups/upload?name=junk.cvbackup", content=b"not a zip")
    assert junk.status_code == 400 and junk.json()["error"]["code"] == "bad_backup"
    assert c.put(f"/api/orgs/{org_id}/backups/upload?name=x.txt", content=b"x").status_code == 400
    # path tricks
    assert c.get(f"/api/orgs/{org_id}/backups/..%2Fsecret_key/download").status_code == 404


@needs_pg_tools
def test_backups_are_for_owners_only(client):
    out = run_setup(client)
    admin = add_member(out["org_id"], "a@example.com", Role.ADMIN, client)
    assert admin.get(f"/api/orgs/{out['org_id']}/backups").status_code == 403
    assert admin.post(f"/api/orgs/{out['org_id']}/backups", json={}).status_code == 403


def test_session_factory_is_usable_after_tests():
    with session_factory()() as db:
        assert db.scalar(text("SELECT 1")) == 1
