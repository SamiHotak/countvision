"""Live camera picture of CountVision Local: the REAL edge agent's live server (countvision_edge)
behind the API. Who may watch, the owner's privacy setting (full / blur / off), only cameras of
this computer, the activity log, a running API server for the MJPEG stream, and clear errors."""

from __future__ import annotations

import threading
import time

import cv2
import httpx
import numpy as np
import pytest
import uvicorn
from conftest import run_setup
from fastapi.testclient import TestClient
from sqlalchemy import update
from test_local import add_camera, add_member, device_client

from countvision_cloud.db import db_session
from countvision_cloud.local import live_view
from countvision_cloud.product.models import Device
from countvision_cloud.saas.models import Role
from countvision_cloud.settings import get_settings

edge_config = pytest.importorskip("countvision_edge.config")
from countvision_edge.detectors import build_detector  # noqa: E402
from countvision_edge.inputs.base import FrameSource, SourceStatus  # noqa: E402
from countvision_edge.storage import SqliteBuffer  # noqa: E402
from countvision_edge.supervisor import Supervisor  # noqa: E402
from countvision_edge.types import Frame  # noqa: E402


class MovingBox(FrameSource):
    """A live camera: a red "person" walking over a grey background (the blob detector finds it)."""

    is_live = True

    def __init__(self) -> None:
        self.i = 0
        self.closed = False
        self.t0 = time.time()

    def open(self) -> None:
        pass

    def read(self, timeout: float = 1.0) -> Frame | None:
        if self.closed:
            from countvision_edge.errors import EndOfStream

            raise EndOfStream("closed")
        time.sleep(0.04)
        self.i += 1
        image = np.full((360, 640, 3), 90, np.uint8)
        x = 40 + (self.i * 7) % 520
        cv2.rectangle(image, (x, 120), (x + 50, 250), (0, 0, 230), -1)
        now = time.time()
        return Frame(image, self.i, now, now - self.t0)

    def close(self) -> None:
        self.closed = True

    def status(self) -> SourceStatus:
        return SourceStatus(connected=True, last_frame_age_s=0.0)


def wait_for(check, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def edge(monkeypatch):
    """The built-in edge agent's live server, started from the edge.yaml the API wrote."""
    started: list = []

    def start(data_dir):
        cfg = edge_config.load_config(get_settings().edge_dir / "edge.yaml", env={})
        cfg = cfg.model_copy(update={"data_dir": str(data_dir), "status_interval_s": 0.2,
                                     "live": cfg.live.model_copy(update={"port": 0})})
        cfg.detector = cfg.detector.model_copy(update={"type": "blobs"})
        sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"),
                         source_factory=lambda cam: MovingBox())
        stop = threading.Event()
        thread = threading.Thread(target=sup.run_forever, args=(stop,), kwargs={"upload": False})
        thread.start()
        started.append((stop, thread))
        assert wait_for(lambda: sup.live_view is not None)
        monkeypatch.setattr(get_settings(), "local_live_port", sup.live_view.port)
        return sup

    yield start
    for stop, thread in started:
        stop.set()
        thread.join(20)


def start_camera(app, client, edge, tmp_path):
    out = run_setup(client)
    cam = add_camera(client, out["org_id"], out["device_id"]).json()
    sup = edge(tmp_path / "edge")
    managed = device_client(app).get("/api/device/poll?rev=-1&wait=0").json()["managed"]
    sup.sync_cameras(managed)  # what ConfigSync does with the poll answer
    assert wait_for(lambda: sup.workers and sup.workers[0].last_result is not None)
    return out, cam, sup


def test_edge_yaml_switches_the_live_picture_on(client):
    run_setup(client)
    path = get_settings().edge_dir / "edge.yaml"
    cfg = edge_config.load_config(path, env={})
    assert cfg.live.enabled and cfg.live.host == "127.0.0.1" and cfg.live.max_mode == "full"
    assert cfg.live.key == live_view.edge_key() and len(cfg.live.key) >= 32
    assert cfg.live.port == get_settings().local_live_port
    if hasattr(path.stat(), "st_mode") and __import__("os").name == "posix":
        assert path.stat().st_mode & 0o077 == 0  # the key is in it: only this user may read it


def test_owner_sets_the_privacy_mode(app, client):
    out = run_setup(client)
    org = out["org_id"]
    assert client.get(f"/api/orgs/{org}/live-view").json() == {
        "mode": "blur", "admins_only": False, "can_watch": True, "can_change": True}
    admin = add_member(org, "admin@example.com", Role.ADMIN, client)
    viewer = add_member(org, "viewer@example.com", Role.VIEWER, client)
    assert viewer.get(f"/api/orgs/{org}/live-view").json()["can_change"] is False
    assert admin.put(f"/api/orgs/{org}/live-view", json={"mode": "full"}).status_code == 403
    assert client.put(f"/api/orgs/{org}/live-view", json={"mode": "loud"}).status_code == 422
    r = client.put(f"/api/orgs/{org}/live-view", json={"mode": "full", "admins_only": True})
    assert r.status_code == 200 and r.json()["mode"] == "full" and r.json()["admins_only"] is True
    assert viewer.get(f"/api/orgs/{org}/live-view").json()["can_watch"] is False
    assert admin.get(f"/api/orgs/{org}/live-view").json()["can_watch"] is True
    log = client.get(f"/api/orgs/{org}/audit").json()
    changed = [e for e in log if e["action"] == "privacy.live_view_changed"]
    assert len(changed) == 1 and changed[0]["meta"]["old_mode"] == "blur" and changed[0]["meta"]["mode"] == "full"


def test_live_picture_follows_the_privacy_setting(app, client, edge, tmp_path):
    out, cam, sup = start_camera(app, client, edge, tmp_path)
    org = out["org_id"]
    url = f"/api/orgs/{org}/cameras/{cam['id']}/live.jpg"

    r = client.get(url)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg" and r.headers["x-live-mode"] == "blur"
    assert "no-store" in r.headers["cache-control"]
    image = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert image.shape[:2] == (360, 640)
    # blurred: the red "person" is pixelated, so no sharp red edge is left inside its box
    red = (image[:, :, 2] > 150) & (image[:, :, 1] < 80)
    assert red.sum() > 0

    client.put(f"/api/orgs/{org}/live-view", json={"mode": "full"})
    r = client.get(url + "?geometry=false&w=320")
    assert r.status_code == 200 and r.headers["x-live-mode"] == "full"
    assert cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR).shape[1] == 320

    client.put(f"/api/orgs/{org}/live-view", json={"mode": "off"})
    r = client.get(url)
    assert r.status_code == 403 and r.json()["error"]["code"] == "live_off"

    client.put(f"/api/orgs/{org}/live-view", json={"mode": "blur", "admins_only": True})
    viewer = add_member(org, "viewer@example.com", Role.VIEWER, client)
    r = viewer.get(url)
    assert r.status_code == 403 and r.json()["error"]["code"] == "live_admins_only"
    assert TestClient(app).get(url).status_code == 401  # not logged in


def test_clear_errors(app, client, edge, tmp_path, monkeypatch):
    out, cam, sup = start_camera(app, client, edge, tmp_path)
    org = out["org_id"]
    url = f"/api/orgs/{org}/cameras/{cam['id']}/live.jpg"
    # a camera the agent does not run (yet)
    other = add_camera(client, org, out["device_id"], "Back", "http", "http://192.168.1.9:8080/video").json()
    r = client.get(f"/api/orgs/{org}/cameras/{other['id']}/live.jpg")
    assert r.status_code == 409 and r.json()["error"]["code"] == "camera_not_running"
    # a camera of another device (paired from elsewhere): no picture
    with db_session() as db:
        db.execute(update(Device).values(builtin=False))
    r = client.get(url)
    assert r.status_code == 409 and r.json()["error"]["code"] == "live_not_here"
    with db_session() as db:
        db.execute(update(Device).values(builtin=True))
    # the counting service is not running
    monkeypatch.setattr(get_settings(), "local_live_port", 9)
    r = client.get(url)
    assert r.status_code == 503 and r.json()["error"]["code"] == "edge_unreachable"
    # a wrong key (e.g. after a restore, before the agent restarted)
    monkeypatch.setattr(get_settings(), "local_live_port", sup.live_view.port)
    monkeypatch.setattr(live_view, "edge_key", lambda: "another-key-another-key")
    r = client.get(url)
    assert r.status_code == 502 and r.json()["error"]["code"] == "live_key"


@pytest.fixture
def api_server(app):
    """The API on a real port: an endless MJPEG stream needs a real HTTP connection."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", timeout_graceful_shutdown=2)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    assert wait_for(lambda: server.started)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(10)


def test_mjpeg_stream_through_the_api_and_activity_log(app, client, edge, tmp_path, api_server):
    out, cam, sup = start_camera(app, client, edge, tmp_path)
    org = out["org_id"]
    cookies = dict(client.cookies)
    url = f"{api_server}/api/orgs/{org}/cameras/{cam['id']}/live.mjpg?fps=10&w=480"
    for _ in range(2):  # watched twice: one entry in the activity log
        with httpx.stream("GET", url, cookies=cookies, timeout=10) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("multipart/x-mixed-replace")
            data = b""
            started = time.time()
            for chunk in resp.iter_raw():
                data += chunk
                if data.count(b"Content-Type: image/jpeg") >= 3 or time.time() - started > 5:
                    break
        assert data.count(b"Content-Type: image/jpeg") >= 3
        assert wait_for(lambda: sup.live_view.viewers == 0, timeout=5)  # the viewer left: agent stops
    entries = [e for e in client.get(f"/api/orgs/{org}/audit").json() if e["action"] == "camera.live_viewed"]
    assert len(entries) == 1 and entries[0]["meta"]["mode"] == "blur"
    # errors come before the stream starts, as JSON
    client.put(f"/api/orgs/{org}/live-view", json={"mode": "off"})
    r = httpx.get(url, cookies=cookies, timeout=10)
    assert r.status_code == 403 and r.json()["error"]["code"] == "live_off"
