"""Cameras managed in the web app (cloud.manage_cameras, CountVision Local's built-in agent):
the supervisor starts, restarts, renames and stops cameras while it runs, keeps the list for the
next start, and picks up a renewed device token."""

from __future__ import annotations

import json
import threading
import time

import pytest

from countvision_edge.cloud import CloudError, CloudUploader, Credentials
from countvision_edge.cloud_sync import ConfigSync, load_managed, load_overlay, save_overlay
from countvision_edge.config import parse_config
from countvision_edge.detectors import build_detector
from countvision_edge.errors import ConfigError
from countvision_edge.storage import SqliteBuffer
from countvision_edge.supervisor import Supervisor

from .test_cloud_sync import DOC, wait_for
from .test_supervisor import FakeLive


def managed_config(tmp_path, **extra):
    data = {"data_dir": str(tmp_path / "edge"), "detector": {"type": "blobs"},
            "cloud": {"manage_cameras": True, "upload_interval_s": 2}, "cameras": [],
            "status_interval_s": 0.2, **extra}
    return parse_config(data, env={})


class Sources:
    """source_factory for the tests: one FakeLive per started camera, remembered by camera id."""

    def __init__(self) -> None:
        self.made: list[tuple[str, str, FakeLive]] = []

    def __call__(self, cam):
        fake = FakeLive()
        self.made.append((cam.id, cam.source.uri, fake))
        return fake

    def for_id(self, camera_id: str) -> list[FakeLive]:
        return [f for cid, _, f in self.made if cid == camera_id]


def spec(cid: str, name: str, uri: str, kind: str = "rtsp") -> dict:
    return {"id": cid, "name": name, "source": {"kind": kind, "uri": uri}}


def test_config_without_cameras_needs_manage_cameras(tmp_path):
    with pytest.raises(ConfigError, match="at least one camera"):
        parse_config({"data_dir": str(tmp_path), "cameras": []}, env={})
    cfg = managed_config(tmp_path)
    assert cfg.cameras == [] and cfg.cloud.manage_cameras
    with pytest.raises(ConfigError, match="web app"):
        cfg.camera()


def test_cameras_come_and_go_while_running(tmp_path):
    cfg = managed_config(tmp_path)
    sources = Sources()
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=sources)
    stop = threading.Event()
    runner = threading.Thread(target=sup.run_forever, args=(stop,), kwargs={"upload": False})
    runner.start()
    try:
        time.sleep(0.5)
        assert runner.is_alive() and sup.workers == []  # no cameras yet: it waits

        sup.sync_cameras([spec("cam-a1", "Entrance", "rtsp://u:pw@10.0.0.5/s1")])
        assert wait_for(lambda: sup.workers and sup.workers[0].refresh().state == "running")
        assert sup.workers[0].cam.name == "Entrance" and sup.workers[0].cam.source.kind == "rtsp"
        saved = load_managed(cfg.data_dir)
        assert saved == {"cam-a1": spec("cam-a1", "Entrance", "rtsp://u:pw@10.0.0.5/s1")}
        status = json.loads(sup.status_path.read_text()) if wait_for(sup.status_path.exists) else {}
        assert "pw" not in json.dumps(status)  # status.json shows the address without the password

        # the same list again changes nothing
        sup.sync_cameras([spec("cam-a1", "Entrance", "rtsp://u:pw@10.0.0.5/s1")])
        assert len(sources.made) == 1

        # a second camera, a renamed first one
        sup.sync_cameras([spec("cam-a1", "Front door", "rtsp://u:pw@10.0.0.5/s1"),
                          spec("cam-b2", "Webcam", "0", "webcam")])
        assert wait_for(lambda: len(sup.workers) == 2 and all(w.refresh().state == "running" for w in sup.workers))
        assert len(sources.for_id("cam-a1")) == 1  # renamed without a restart
        assert sup.workers[0].health.name == "Front door" and sup.workers[0].cam.name == "Front door"
        assert sup.cloud_status()["cameras"][0]["name"] == "Front door"

        # a new password: that camera restarts with the new address
        old = sources.for_id("cam-a1")[0]
        sup.sync_cameras([spec("cam-a1", "Front door", "rtsp://u:new@10.0.0.5/s1"),
                          spec("cam-b2", "Webcam", "0", "webcam")])
        assert wait_for(lambda: len(sources.for_id("cam-a1")) == 2)
        assert wait_for(lambda: old.closed)
        assert sources.made[-1][1] == "rtsp://u:new@10.0.0.5/s1"

        # removed in the web app: stopped, gone from the status, the other keeps counting
        webcam = sources.for_id("cam-b2")[0]
        sup.sync_cameras([spec("cam-b2", "Webcam", "0", "webcam")])
        assert [w.cam.id for w in sup.workers] == ["cam-b2"]
        assert wait_for(lambda: sources.for_id("cam-a1")[-1].closed)
        assert not webcam.closed
        assert list(load_managed(cfg.data_dir)) == ["cam-b2"]
        assert runner.is_alive()
    finally:
        stop.set()
        runner.join(10)
    assert not runner.is_alive()
    assert all(f.closed for _, _, f in sources.made)

    # next start: the saved camera starts at once, before the cloud answered
    again = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=Sources())
    assert [w.cam.id for w in again.workers] == ["cam-b2"] and again.workers[0].cam.name == "Webcam"


def test_bad_entries_are_skipped_and_yaml_ids_win(tmp_path):
    cfg = parse_config({"data_dir": str(tmp_path / "e"), "detector": {"type": "blobs"},
                        "cloud": {"manage_cameras": True},
                        "cameras": [{"id": "door", "source": {"uri": "rtsp://cam/1"}}]}, env={})
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=Sources())
    sup.sync_cameras([spec("bad id with spaces", "X", "rtsp://cam/2"), spec("door", "Clash", "rtsp://cam/3"),
                      {"name": "no id"}, spec("cam-ok", "Fine", "rtsp://cam/4")])
    assert [w.cam.id for w in sup.workers] == ["door", "cam-ok"]
    assert sup.workers[0].cam.source.uri == "rtsp://cam/1"


def test_config_sync_starts_a_camera_then_applies_its_lines(tmp_path):
    cfg = managed_config(tmp_path)
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=Sources())
    answers = [{"rev": 1, "changed": True, "snapshots": [], "managed": [spec("cam-x", "Till", "http://cam/v", "http")],
                "cameras": [{"id": "cam-x", "version": 1, "config": DOC, "timezone": "Europe/Berlin"}]}]

    def http(method, url, body=None, token=None, timeout=20.0):
        return answers.pop(0) if answers else {"rev": 1, "changed": False}

    creds = Credentials(url="http://127.0.0.1:9", device_id="d", token="cvd_x")
    sync = ConfigSync(creds, apply_config=sup.apply_camera_config, take_snapshot=sup.snapshot, http=http,
                      sync_cameras=sup.sync_cameras)
    assert sync.poll_once() is True
    worker = sup.workers[0]
    assert worker.cam.id == "cam-x" and worker.cam.lines[0].name == "entrance"
    assert worker.health.config_version == 1
    overlay = json.loads((tmp_path / "edge" / "cloud_config.json").read_text())
    assert overlay["managed"][0]["id"] == "cam-x" and overlay["cameras"]["cam-x"]["version"] == 1
    # a restart keeps both: the camera and its lines
    again = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=Sources())
    assert again.workers[0].cam.lines[0].name == "entrance"


def test_saving_configs_keeps_the_managed_list(tmp_path):
    save_overlay(tmp_path, {}, managed=[spec("cam-1", "A", "rtsp://x/1")])
    save_overlay(tmp_path, {"cam-1": {"version": 2, "config": DOC}})  # a config change only
    assert list(load_managed(tmp_path)) == ["cam-1"] and load_overlay(tmp_path)["cam-1"]["version"] == 2


def test_uploader_and_config_sync_use_a_renewed_token(tmp_path):
    old = Credentials(url="http://127.0.0.1:9", device_id="d", token="cvd_old")
    old.save(tmp_path)

    def refuse(payload):
        raise CloudError("This device was removed", status=401, code="device_token_invalid")

    up = CloudUploader(SqliteBuffer(":memory:"), old, send=refuse, data_dir=tmp_path)
    assert up._on_error(CloudError("x", status=401)) is None  # same token in the file: stop
    assert up.state.state == "revoked"
    Credentials(url="http://127.0.0.1:9", device_id="d", token="cvd_new").save(tmp_path)
    assert up._on_error(CloudError("x", status=401)) == 1.0
    assert up.creds.token == "cvd_new" and up.state.state == "starting"

    calls = []

    def http(method, url, body=None, token=None, timeout=20.0):
        calls.append(token)
        if token == "cvd_old":
            raise CloudError("revoked", status=401)
        sync.stop()
        return {"rev": 3, "changed": False}

    sync = ConfigSync(old, apply_config=lambda *a: None, take_snapshot=lambda c: b"", http=http,
                      data_dir=tmp_path, wait_s=0.1)
    sync.start()
    sync.join(5)
    assert calls == ["cvd_old", "cvd_new"] and sync.creds.token == "cvd_new"
