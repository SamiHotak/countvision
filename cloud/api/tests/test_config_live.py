"""Phase 3 C: camera config push (long-poll), snapshots on request, live counters (SSE)."""

from __future__ import annotations

import json
import threading
import time
from datetime import timedelta

from conftest import signup
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_devices import batch, setup
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import Device
from countvision_cloud.saas.models import Role

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000 + b"\xff\xd9"

CONFIG = {
    "lines": [{"name": "Door", "p1": [0.5, 0.1], "p2": [0.5, 0.9], "in_direction": "to_left"}],
    "zones": [{"name": "Queue", "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.5]], "kind": "queue"}],
    "classes": ["person", "bicycle"],
    "anchor": "center",
    "schedule": {"days": [0, 1, 2, 3, 4, 5], "start": "08:00", "end": "20:00"},
}


def camera_id(client, org, body) -> str:
    detail = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()
    return detail["cameras"][0]["id"]


def reported(**extra) -> dict:
    cam = {"id": "door", "name": "Entrance", "state": "running", "connected": True,
           "config": {"lines": [{"name": "entrance", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}], "classes": ["person"]},
           "snapshots_allowed": True, "frame": [1280, 720]}
    cam.update(extra)
    return cam


def test_reported_config_then_cloud_edit_then_applied(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch(cameras=[reported(config_version=0)]))
    cid = camera_id(client, org, body)
    detail = client.get(f"/api/orgs/{org['id']}/cameras/{cid}").json()
    assert detail["config_source"] == "device" and detail["config"]["lines"][0]["name"] == "entrance"
    assert detail["frame_width"] == 1280 and detail["snapshots_allowed"] is True

    # the device has nothing to do yet
    first = dev.get("/api/device/poll?rev=-1&wait=0").json()
    assert first["changed"] is True and first["cameras"] == [] and first["snapshots"] == []
    assert dev.get(f"/api/device/poll?rev={first['rev']}&wait=0").json() == {"rev": first["rev"], "changed": False}

    saved = client.put(f"/api/orgs/{org['id']}/cameras/{cid}/config", json=CONFIG).json()
    assert saved["config_source"] == "cloud" and saved["desired_version"] == 1 and saved["applied_version"] == 0
    poll = dev.get(f"/api/device/poll?rev={first['rev']}&wait=0").json()
    assert poll["changed"] and poll["cameras"][0]["id"] == "door" and poll["cameras"][0]["version"] == 1
    assert poll["cameras"][0]["config"]["anchor"] == "center"
    assert poll["cameras"][0]["timezone"] == "Europe/Berlin"

    # the edge applies it and reports the version with the next upload
    dev.post("/api/device/ingest", json=batch("b2", cameras=[reported(config_version=1, config=CONFIG)]))
    detail = client.get(f"/api/orgs/{org['id']}/cameras/{cid}").json()
    assert detail["applied_version"] == 1 and detail["config_source"] == "cloud"
    actions = [e["action"] for e in client.get(f"/api/orgs/{org['id']}/audit").json()]
    assert "camera.config_saved" in actions


def test_device_page_shows_config_versions_and_paused_cameras(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch(cameras=[reported(config_version=0)]))
    cid = camera_id(client, org, body)
    client.put(f"/api/orgs/{org['id']}/cameras/{cid}/config", json=CONFIG)
    cam = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()["cameras"][0]
    assert (cam["desired_version"], cam["applied_version"], cam["config_error"]) == (1, 0, None)
    # outside the counting hours the edge reports "paused": that is not a camera problem
    dev.post("/api/device/ingest", json=batch("b2", cameras=[reported(state="paused", config_version=1, config=CONFIG)]))
    device = client.get(f"/api/orgs/{org['id']}/devices").json()[0]
    assert device["cameras_online"] == 1
    cam = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()["cameras"][0]
    assert cam["state"] == "paused" and cam["applied_version"] == 1


def test_config_validation(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch())
    cid = camera_id(client, org, body)
    url = f"/api/orgs/{org['id']}/cameras/{cid}/config"
    bad = [
        {"lines": [{"name": "a", "p1": [0.5, 0.5], "p2": [0.5, 0.5]}]},  # too short
        {"lines": [{"name": "a", "p1": [0.1, 0.1], "p2": [1.5, 0.5]}]},  # outside the picture
        {"lines": [{"name": "a", "p1": [0, 0], "p2": [1, 1]}, {"name": "a", "p1": [0, 1], "p2": [1, 0]}]},
        {"zones": [{"name": "z", "polygon": [[0, 0], [1, 1]]}]},  # 2 points
        {"classes": ["unicorn"]},
        {"schedule": {"days": [7], "start": "08:00", "end": "20:00"}},
        {"schedule": {"days": [1], "start": "8am", "end": "20:00"}},
    ]
    for doc in bad:
        assert client.put(url, json=doc).status_code == 422, doc


def test_long_poll_wakes_up_on_change(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch())
    cid = camera_id(client, org, body)
    rev = dev.get("/api/device/poll?rev=-1&wait=0").json()["rev"]
    result: dict = {}

    def waiter():
        started = time.monotonic()
        result["answer"] = TestClient(app, headers=dict(dev.headers)).get(f"/api/device/poll?rev={rev}&wait=8").json()
        result["seconds"] = time.monotonic() - started

    thread = threading.Thread(target=waiter)
    thread.start()
    time.sleep(1.0)
    client.put(f"/api/orgs/{org['id']}/cameras/{cid}/config", json=CONFIG)
    thread.join(10)
    assert result["answer"]["changed"] is True and result["answer"]["cameras"][0]["version"] == 1
    assert result["seconds"] < 3.0  # answered right after the save, not after 8 s


def test_viewer_cannot_edit_or_snapshot(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch(cameras=[reported()]))
    cid = camera_id(client, org, body)
    signup(client2, "viewer@example.com")
    add_member(org["id"], "viewer@example.com", Role.VIEWER)
    assert client2.get(f"/api/orgs/{org['id']}/cameras/{cid}").status_code == 200
    assert client2.put(f"/api/orgs/{org['id']}/cameras/{cid}/config", json=CONFIG).status_code == 403
    assert client2.post(f"/api/orgs/{org['id']}/cameras/{cid}/snapshot").status_code == 403


def test_snapshot_flow(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch(cameras=[reported()]))
    cid = camera_id(client, org, body)
    base = f"/api/orgs/{org['id']}/cameras/{cid}/snapshot"
    req = client.post(base).json()
    assert req["status"] == "pending"
    rid = req["request_id"]
    assert client.get(f"{base}/{rid}").json()["status"] == "pending"
    assert client.get(f"{base}/{rid}/image").status_code == 404

    poll = dev.get("/api/device/poll?rev=-1&wait=0").json()
    assert poll["snapshots"] == [{"request_id": rid, "camera_id": "door"}]
    up = dev.post(f"/api/device/snapshots/{rid}", content=JPEG, headers={"Content-Type": "image/jpeg"})
    assert up.status_code == 204
    assert client.get(f"{base}/{rid}").json()["status"] == "ready"
    image = client.get(f"{base}/{rid}/image")
    assert image.status_code == 200 and image.content == JPEG and image.headers["content-type"] == "image/jpeg"
    assert dev.get("/api/device/poll?rev=-1&wait=0").json()["snapshots"] == []  # done
    # a second upload for the same request is refused, as is a fake request id
    assert dev.post(f"/api/device/snapshots/{rid}", content=JPEG).status_code == 404
    assert dev.post("/api/device/snapshots/nope", content=JPEG).status_code == 404

    # device-side error (e.g. no frame yet) is shown to the user
    rid2 = client.post(base).json()["request_id"]
    dev.post(f"/api/device/snapshots/{rid2}", json={"error": "No picture yet: the camera is still connecting."})
    status = client.get(f"{base}/{rid2}").json()
    assert status["status"] == "error" and "connecting" in status["message"]
    # not a JPEG
    rid3 = client.post(base).json()["request_id"]
    dev.post(f"/api/device/snapshots/{rid3}", content=b"<html>", headers={"Content-Type": "image/jpeg"})
    assert client.get(f"{base}/{rid3}").json()["status"] == "error"


def test_snapshot_refused_when_offline_or_disabled(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch(cameras=[reported(snapshots_allowed=False)]))
    cid = camera_id(client, org, body)
    off = client.post(f"/api/orgs/{org['id']}/cameras/{cid}/snapshot")
    assert off.status_code == 403 and off.json()["error"]["code"] == "snapshots_disabled"
    dev.post("/api/device/ingest", json=batch("b2", cameras=[reported()]))
    with session_factory()() as db:
        db.execute(update(Device).values(last_seen_at=utcnow() - timedelta(minutes=5)))
        db.commit()
    offline = client.post(f"/api/orgs/{org['id']}/cameras/{cid}/snapshot")
    assert offline.status_code == 409 and offline.json()["error"]["code"] == "device_offline"


def test_today_counts_come_from_events(app, client):
    org, site, code, dev, body = setup(client, app)
    now = time.time()
    events = [{"event_id": f"e{i}", "camera_id": "door", "kind": "line_cross", "ts": now - i, "name": "entrance",
               "class_name": "person", "direction": "in" if i % 3 else "out"} for i in range(9)]
    events.append({"event_id": "z1", "camera_id": "door", "kind": "zone_visit", "ts": now, "name": "queue"})
    dev.post("/api/device/ingest", json=batch(events=events))
    dev.post("/api/device/ingest", json=batch("b2", events=events))  # replay: no doubles
    cid = camera_id(client, org, body)
    assert client.get(f"/api/orgs/{org['id']}/cameras/{cid}").json()["today"] == {"entrance": {"in": 6, "out": 3}}


def read_sse(app, client, org_id: str, seconds: float, out: list) -> None:
    with TestClient(app, cookies=client.cookies) as c, c.stream("GET", f"/api/orgs/{org_id}/live?max_s={seconds}") as resp:
        assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/event-stream")
        for line in resp.iter_lines():
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))


def test_live_stream_sends_new_crossings_once(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    got: list = []
    reader = threading.Thread(target=read_sse, args=(app, client, org["id"], 3, got))
    reader.start()
    time.sleep(1.0)
    ev = [{"event_id": "x1", "camera_id": "door", "kind": "line_cross", "ts": time.time(), "name": "entrance",
           "class_name": "person", "direction": "in"}]
    dev.post("/api/device/ingest", json=batch(events=ev))
    dev.post("/api/device/ingest", json=batch("b2", events=ev))  # repeated: no second live count
    reader.join(6)
    types = [m["type"] for m in got]
    assert types[0] == "hello"
    counts = [m for m in got if m["type"] == "count"]
    assert len(counts) == 1 and counts[0]["line"] == "entrance" and counts[0]["direction"] == "in"
    assert types.count("device") == 2
    # other organizations get nothing; no login = 401
    signup(client2, "other@example.com")
    assert client2.get(f"/api/orgs/{org['id']}/live?max_s=1").status_code == 404
    assert TestClient(app).get(f"/api/orgs/{org['id']}/live?max_s=1").status_code == 401


def test_poll_needs_a_device_token(app):
    assert TestClient(app).get("/api/device/poll?wait=0").status_code == 401
