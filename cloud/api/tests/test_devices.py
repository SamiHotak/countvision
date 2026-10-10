"""Sites, device pairing, device tokens, ingest (idempotent), device status."""

from __future__ import annotations

import time
import uuid
from datetime import timedelta

from conftest import create_org, signup
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import CountEvent, Device, LineCount, PairingCode, ZoneStat
from countvision_cloud.saas.models import Role


def make_site(client, org_id: str, name: str = "Shop Mitte") -> dict:
    resp = client.post(f"/api/orgs/{org_id}/sites", json={"name": name, "timezone": "Europe/Berlin"})
    assert resp.status_code == 201, resp.text
    return resp.json()


def new_code(client, org_id: str, site_id: str, name: str = "Mini PC 1") -> dict:
    resp = client.post(f"/api/orgs/{org_id}/pairing-codes", json={"site_id": site_id, "device_name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def pair(app, code: str, edge_id: str = "edge-abc123") -> tuple[TestClient, dict]:
    """A device: plain HTTP client WITHOUT cookies or CSRF header (like the edge agent)."""
    dev = TestClient(app)
    resp = dev.post("/api/device/pair", json={"code": code, "edge_device_id": edge_id, "agent_version": "0.5.0"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    dev.headers["Authorization"] = f"Bearer {body['token']}"
    return dev, body


def setup(client, app):
    signup(client, "owner@example.com", "Olga")
    org = create_org(client, "Café Sonne")
    site = make_site(client, org["id"])
    code = new_code(client, org["id"], site["id"])
    dev, body = pair(app, code["code"])
    return org, site, code, dev, body


def minute(offset_min: int = 0) -> int:
    now = int(time.time())
    return now - now % 60 - 60 * offset_min


def batch(batch_id: str = "b1", **rows) -> dict:
    data = {"batch_id": batch_id, "agent_version": "0.5.0", "sent_at": time.time(),
            "status": {"version": "0.5.0", "detector": {"name": "yolox_tiny", "license": "Apache-2.0"},
                       "upload": {"pending": 0}, "cameras": "ignored"},
            "cameras": [{"id": "door", "name": "Entrance", "state": "running", "connected": True, "fps": 9.8,
                         "reconnects": 0, "lines": ["entrance"], "zones": ["queue"]}]}
    data.update(rows)
    return data


def test_sites_crud_and_roles(client, client2):
    signup(client, "owner@example.com")
    org = create_org(client)
    site = make_site(client, org["id"], "Filiale Nord")
    assert site["timezone"] == "Europe/Berlin" and site["device_count"] == 0
    bad = client.post(f"/api/orgs/{org['id']}/sites", json={"name": "X", "timezone": "Mars/Base"})
    assert bad.status_code == 422 and "timezone" in bad.json()["error"]["fields"]
    renamed = client.patch(f"/api/orgs/{org['id']}/sites/{site['id']}", json={"name": "Filiale Süd"})
    assert renamed.json()["name"] == "Filiale Süd"
    # a viewer can read but not create
    signup(client2, "viewer@example.com")
    add_member(org["id"], "viewer@example.com", Role.VIEWER)
    assert len(client2.get(f"/api/orgs/{org['id']}/sites").json()) == 1
    assert client2.post(f"/api/orgs/{org['id']}/sites", json={"name": "Y"}).status_code == 403
    assert client2.post(f"/api/orgs/{org['id']}/pairing-codes",
                        json={"site_id": site["id"], "device_name": "x"}).status_code == 403
    assert client.delete(f"/api/orgs/{org['id']}/sites/{site['id']}").status_code == 200


def test_pairing_flow(client, app):
    signup(client, "owner@example.com")
    org = create_org(client, "Café Sonne")
    site = make_site(client, org["id"])
    code = new_code(client, org["id"], site["id"], "Kasse PC")
    assert len(code["code"]) == 9 and code["code"][4] == "-" and code["status"] == "pending"
    status = client.get(f"/api/orgs/{org['id']}/pairing-codes/{code['id']}").json()
    assert status["code"] is None and status["status"] == "pending"  # the code is shown only once

    # typed lower case and without the dash: still works
    dev, body = pair(app, code["code"].replace("-", "").lower())
    assert body["org_name"] == "Café Sonne" and body["site_name"] == "Shop Mitte"
    assert body["device_name"] == "Kasse PC" and body["token"].startswith("cvd_")
    status = client.get(f"/api/orgs/{org['id']}/pairing-codes/{code['id']}").json()
    assert status["status"] == "used" and status["device_id"] == body["device_id"]

    again = TestClient(app).post("/api/device/pair", json={"code": code["code"], "edge_device_id": "edge-2"})
    assert again.status_code == 400 and again.json()["error"]["code"] == "invalid_code"
    assert dev.get("/api/device/me").json()["site"] == "Shop Mitte"
    with session_factory()() as db:
        device = db.scalar(select(Device))
        assert device.token_hash != body["token"] and len(device.token_hash) == 64  # stored hashed


def test_expired_and_wrong_codes_and_rate_limit(client, app):
    signup(client, "owner@example.com")
    org = create_org(client)
    site = make_site(client, org["id"])
    code = new_code(client, org["id"], site["id"])
    with session_factory()() as db:
        db.execute(update(PairingCode).values(expires_at=utcnow() - timedelta(seconds=1)))
        db.commit()
    dev = TestClient(app)
    assert dev.post("/api/device/pair", json={"code": code["code"], "edge_device_id": "e1"}).status_code == 400
    for _ in range(9):
        dev.post("/api/device/pair", json={"code": "AAAA-AAAA", "edge_device_id": "e1"})
    assert dev.post("/api/device/pair", json={"code": "AAAA-AAAA", "edge_device_id": "e1"}).status_code == 429


def test_device_auth(app, client):
    org, site, code, dev, body = setup(client, app)
    anon = TestClient(app)
    assert anon.post("/api/device/ingest", json=batch()).status_code == 401
    bad = TestClient(app, headers={"Authorization": "Bearer cvd_wrong"})
    assert bad.post("/api/device/ingest", json=batch()).status_code == 401
    # a logged-in user cookie is NOT a device token
    assert client.post("/api/device/ingest", json=batch()).status_code == 401
    assert dev.post("/api/device/ingest", json=batch()).status_code == 200


def test_ingest_is_idempotent_and_keeps_newest_minute_value(app, client):
    org, site, code, dev, body = setup(client, app)
    w = minute(2)
    rows = dict(
        line_counts=[{"camera_id": "door", "window_start": w, "line": "entrance", "class_name": "*",
                      "in_count": 3, "out_count": 1, "id": 7, "sent": 0},  # extra edge fields are ignored
                     {"camera_id": "door", "window_start": w, "line": "entrance", "class_name": "person",
                      "in_count": 3, "out_count": 1}],
        zone_stats=[{"camera_id": "door", "window_start": w, "zone": "queue", "class_name": "*", "sample_s": 60,
                     "occ_avg": 1.5, "occ_max": 3, "occ_last": 1, "queue_avg": 0.5, "queue_max": 2, "visits": 2,
                     "dwell_sum_s": 40, "dwell_max_s": 25}],
        coverage=[{"camera_id": "door", "window_start": w, "frames": 600, "seconds": 60}],
        events=[{"event_id": "e1", "camera_id": "door", "kind": "line_cross", "ts": w + 10, "name": "entrance",
                 "class_name": "person", "direction": "in", "track_id": 4}],
        heartbeats=[{"camera_id": "door", "ts": time.time(), "fps": 9.7, "connected": 1, "cpu_pct": 40}],
    )
    first = dev.post("/api/device/ingest", json=batch("b1", **rows)).json()
    assert first["accepted"] == {"line_counts": 2, "zone_stats": 1, "coverage": 1, "events": 1, "heartbeats": 1}
    assert first["duplicate_batch"] is False and first["rejected"] == 0
    second = dev.post("/api/device/ingest", json=batch("b1", **rows)).json()  # network retry
    assert second["duplicate_batch"] is True

    # later the same minute grows (late update on the edge): newest value wins, nothing doubles
    rows["line_counts"][0]["in_count"] = 5
    dev.post("/api/device/ingest", json=batch("b2", **rows))
    with session_factory()() as db:
        totals = db.execute(select(func.sum(LineCount.in_count)).where(LineCount.class_name == "*")).scalar()
        assert totals == 5
        assert db.scalar(select(func.count()).select_from(CountEvent)) == 1
        assert db.scalar(select(func.count()).select_from(ZoneStat)) == 1

    detail = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()
    assert detail["online"] is True and detail["agent_version"] == "0.5.0"
    assert detail["detector"]["name"] == "yolox_tiny"
    cam = detail["cameras"][0]
    assert cam["name"] == "Entrance" and cam["state"] == "running" and cam["fps"] == 9.8
    assert cam["lines"] == ["entrance"] and cam["today"]["entrance"] == {"in": 1, "out": 0}  # from the crossing events
    assert detail["batches_24h"] == 2 and detail["camera_count"] == 1 and detail["cameras_online"] == 1


def test_ingest_rejects_impossible_times_and_bad_rows(app, client):
    org, site, code, dev, body = setup(client, app)
    future = int(time.time()) + 3 * 86400
    resp = dev.post("/api/device/ingest", json=batch(line_counts=[
        {"camera_id": "door", "window_start": future, "line": "entrance", "class_name": "*", "in_count": 1,
         "out_count": 0},
        {"camera_id": "door", "window_start": minute(1), "line": "entrance", "class_name": "*", "in_count": 2,
         "out_count": 0}])).json()
    assert resp["rejected"] == 1 and resp["accepted"]["line_counts"] == 1
    neg = dev.post("/api/device/ingest", json=batch(line_counts=[
        {"camera_id": "door", "window_start": minute(1), "line": "x", "class_name": "*", "in_count": -1,
         "out_count": 0}]))
    assert neg.status_code == 422
    too_many = dev.post("/api/device/ingest", json=batch(cameras=[{"id": f"c{i}"} for i in range(65)]))
    assert too_many.status_code == 422


def test_large_batch_is_chunked(app, client):
    org, site, code, dev, body = setup(client, app)
    base = minute(4000)
    rows = [{"camera_id": "door", "window_start": base + 60 * i, "zone": "queue", "class_name": "*",
             "sample_s": 60, "occ_avg": 1, "occ_max": 1, "occ_last": 1, "queue_avg": 0, "queue_max": 0,
             "visits": 0, "dwell_sum_s": 0, "dwell_max_s": 0} for i in range(5000)]
    resp = dev.post("/api/device/ingest", json=batch(zone_stats=rows))
    assert resp.status_code == 200 and resp.json()["accepted"]["zone_stats"] == 5000


def test_camera_rename_sticks_and_device_list(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch())
    detail = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()
    cam_id = detail["cameras"][0]["id"]
    assert client.patch(f"/api/orgs/{org['id']}/cameras/{cam_id}", json={"name": "Haupteingang"}).status_code == 200
    dev.post("/api/device/ingest", json=batch("b2"))  # edge still calls it "Entrance"
    detail = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()
    assert detail["cameras"][0]["name"] == "Haupteingang"
    devices = client.get(f"/api/orgs/{org['id']}/devices").json()
    assert devices[0]["name"] == "Mini PC 1" and devices[0]["site_name"] == "Shop Mitte"
    sites = client.get(f"/api/orgs/{org['id']}/sites").json()
    assert sites[0]["device_count"] == 1 and sites[0]["camera_count"] == 1


def test_offline_after_90_seconds(app, client):
    org, site, code, dev, body = setup(client, app)
    dev.post("/api/device/ingest", json=batch())
    with session_factory()() as db:
        db.execute(update(Device).values(last_seen_at=utcnow() - timedelta(seconds=120)))
        db.commit()
    detail = client.get(f"/api/orgs/{org['id']}/devices/{body['device_id']}").json()
    assert detail["online"] is False and detail["cameras"][0]["state"] == "offline"
    assert detail["cameras_online"] == 0


def test_revoke_move_delete(app, client):
    org, site, code, dev, body = setup(client, app)
    did = body["device_id"]
    other = make_site(client, org["id"], "Lager")
    moved = client.patch(f"/api/orgs/{org['id']}/devices/{did}", json={"site_id": other["id"], "name": "PC 2"})
    assert moved.json()["site_name"] == "Lager" and moved.json()["name"] == "PC 2"
    blocked = client.delete(f"/api/orgs/{org['id']}/sites/{other['id']}")
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "site_not_empty"
    assert client.delete(f"/api/orgs/{org['id']}/devices/{did}").status_code == 409  # revoke first
    assert client.post(f"/api/orgs/{org['id']}/devices/{did}/revoke").status_code == 200
    gone = dev.post("/api/device/ingest", json=batch())
    assert gone.status_code == 401 and gone.json()["error"]["code"] == "device_revoked"
    assert client.delete(f"/api/orgs/{org['id']}/devices/{did}").status_code == 200
    assert client.get(f"/api/orgs/{org['id']}/devices").json() == []
    actions = [e["action"] for e in client.get(f"/api/orgs/{org['id']}/audit").json()]
    assert {"device.paired", "device.updated", "device.revoked", "device.deleted"} <= set(actions)


def test_other_org_cannot_see_device(app, client, client2):
    org, site, code, dev, body = setup(client, app)
    signup(client2, "other@example.com")
    other = create_org(client2, "Other")
    assert client2.get(f"/api/orgs/{other['id']}/devices/{body['device_id']}").status_code == 404
    assert client2.get(f"/api/orgs/{org['id']}/devices").status_code == 404
    unknown = uuid.uuid4()
    assert client.get(f"/api/orgs/{org['id']}/devices/{unknown}").status_code == 404
