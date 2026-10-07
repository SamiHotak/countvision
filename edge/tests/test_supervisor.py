"""Supervisor: several cameras at once, restarts, offline alerts, status.json, health."""

from __future__ import annotations

import json
import threading
import time

import numpy as np
import pytest

from countvision_edge import cli
from countvision_edge.config import parse_config
from countvision_edge.detectors import build_detector
from countvision_edge.errors import EndOfStream
from countvision_edge.inputs.base import FrameSource, SourceStatus
from countvision_edge.storage import SqliteBuffer
from countvision_edge.supervisor import SharedDetector, Supervisor, check_health
from countvision_edge.types import Frame


def site_config(tmp_path, video, cameras=("door", "till"), **extra):
    data = {
        "data_dir": str(tmp_path / "data"),
        "detector": {"type": "blobs"},
        "heartbeat_interval_s": 3600,
        "status_interval_s": 0.2,
        "cameras": [
            {
                "id": cam_id,
                "source": {"uri": str(video), "start_time": "2026-03-01T10:00:00"},
                "classes": ["person", "car"],
                "lines": [{"name": "door", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}],
            }
            for cam_id in cameras
        ],
        **extra,
    }
    return parse_config(data, env={})


def test_two_file_cameras_count_in_parallel(tmp_path, demo_video):
    cfg = site_config(tmp_path, demo_video)
    buffer = SqliteBuffer(tmp_path / "db.sqlite")
    sup = Supervisor(cfg, build_detector(cfg.detector), buffer)
    sup.run_forever()
    for worker in sup.workers:
        assert worker.health.state == "finished"
        lines = [r for r in buffer.query("line_counts", camera_id=worker.cam.id) if r["class_name"] == "*"]
        assert sum(r["in_count"] for r in lines) == 4 and sum(r["out_count"] for r in lines) == 2
    status = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    assert [c["camera_id"] for c in status["cameras"]] == ["door", "till"]
    assert all(c["frames_processed"] == 270 for c in status["cameras"])
    assert status["privacy"] == {"preview": "blur", "stores_images": False}
    buffer.close()


def test_a_missing_file_fails_only_that_camera(tmp_path, demo_video):
    cfg = site_config(tmp_path, demo_video)
    cfg.cameras[1].source.uri = str(tmp_path / "missing.avi")
    buffer = SqliteBuffer(":memory:")
    sup = Supervisor(cfg, build_detector(cfg.detector), buffer)
    sup.run_forever()
    assert [w.health.state for w in sup.workers] == ["finished", "failed"]
    assert "not found" in sup.workers[1].health.last_error
    report = check_health(sup.status_path)
    assert report.exit_code == 2 and "failed" in "\n".join(report.lines)


class FakeLive(FrameSource):
    """A live camera that can be switched on and off by the test."""

    is_live = True

    def __init__(self) -> None:
        self.online = threading.Event()
        self.online.set()
        self.closed = False
        self.index = 0
        self.t0 = time.time()

    def open(self) -> None:
        pass

    def read(self, timeout: float = 1.0) -> Frame | None:
        if self.closed:
            raise EndOfStream("closed")
        if not self.online.is_set():
            time.sleep(min(timeout, 0.02))
            return None
        time.sleep(0.01)
        self.index += 1
        now = time.time()
        return Frame(np.zeros((90, 160, 3), np.uint8), self.index, now, now - self.t0)

    def close(self) -> None:
        self.closed = True

    def status(self) -> SourceStatus:
        return SourceStatus(connected=self.online.is_set(), last_frame_age_s=0.0)


def test_offline_alert_after_the_limit_and_back_online(tmp_path, demo_video):
    cfg = site_config(tmp_path, demo_video, cameras=("cam1",), alerts={"camera_offline_after_s": 300})
    cfg.cameras[0].source.kind = "rtsp"
    fake = FakeLive()
    now = [1000.0]
    buffer = SqliteBuffer(":memory:")
    sup = Supervisor(cfg, build_detector(cfg.detector), buffer, source_factory=lambda cam: fake,
                     clock=lambda: now[0])
    sup.start()
    try:
        deadline = time.time() + 5
        while sup.workers[0].refresh().state != "running" and time.time() < deadline:
            time.sleep(0.02)
        assert sup.check_alerts() == []
        fake.online.clear()
        assert sup.check_alerts() == []  # offline, but not long enough for an alert
        assert sup.workers[0].health.state == "offline"
        now[0] += 299
        assert sup.check_alerts() == []
        now[0] += 2
        events = sup.check_alerts()
        assert [e.kind for e in events] == ["camera_offline"]
        assert "offline for more than 5 min" in sup.workers[0].health.alert
        assert sup.check_alerts() == []  # only one alert per outage
        sup.write_status()
        assert check_health(sup.status_path, now=now[0]).exit_code == 2
        fake.online.set()
        now[0] += 10
        events = sup.check_alerts()
        assert [e.kind for e in events] == ["camera_online"] and events[0].dwell_s == pytest.approx(311)
        assert sup.workers[0].health.state == "running" and sup.workers[0].health.alert is None
    finally:
        sup.stop()
        sup.join(5)
    kinds = [e["kind"] for e in buffer.query("events")]
    assert kinds.count("camera_offline") == 1 and kinds.count("camera_online") == 1


class CrashOnce:
    """Detector that raises on the 5th call, then works."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0
        self.names, self.info = inner.names, inner.info

    def detect(self, image):
        self.calls += 1
        if self.calls == 5:
            raise RuntimeError("driver hiccup")
        return self.inner.detect(image)

    def warmup(self):
        pass

    def close(self):
        pass


def test_a_crashing_camera_is_restarted(tmp_path, demo_video, monkeypatch):
    cfg = site_config(tmp_path, demo_video, cameras=("cam1",))
    cfg.cameras[0].source.kind = "rtsp"
    monkeypatch.setattr("countvision_edge.supervisor.Backoff.next_delay", lambda self: 0.05)
    fake = FakeLive()
    sup = Supervisor(cfg, CrashOnce(build_detector(cfg.detector)), SqliteBuffer(":memory:"),
                     source_factory=lambda cam: fake)
    sup.start()
    try:
        deadline = time.time() + 5
        while (sup.workers[0].health.restarts < 1 or sup.workers[0].refresh().state != "running") \
                and time.time() < deadline:
            fake.closed = False
            time.sleep(0.02)
        h = sup.workers[0].health
        assert h.restarts == 1 and h.state == "running" and "driver hiccup" in h.last_error
    finally:
        sup.stop()
        sup.join(5)
    assert sup.workers[0].health.state == "stopped"


def test_a_new_live_camera_is_connecting_not_offline(tmp_path, demo_video):
    cfg = site_config(tmp_path, demo_video, cameras=("cam1",))
    cfg.cameras[0].source.kind = "rtsp"
    fake = FakeLive()
    fake.online.clear()  # the stream is still being opened
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"),
                     source_factory=lambda cam: fake)
    sup.start()
    try:
        time.sleep(0.2)
        assert sup.check_alerts() == []
        assert sup.workers[0].health.state == "connecting"  # not "offline" during the first seconds
        sup.workers[0].connect_deadline = 0  # time is up
        sup.check_alerts()
        assert sup.workers[0].health.state == "offline"
    finally:
        sup.stop()
        sup.join(5)


def test_privacy_env_is_set_before_libraries_load():
    import os

    import countvision_edge

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"  # ONNX Runtime would phone home otherwise
    assert os.environ["YOLO_OFFLINE"] == "1"
    assert set(countvision_edge.PRIVACY_ENV) == {"ORT_DISABLE_TELEMETRY", "YOLO_OFFLINE"}


def test_shared_detector_serialises_calls():
    active, peak = [0], [0]

    class Slow:
        names, info = {0: "person"}, None

        def detect(self, image):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            time.sleep(0.01)
            active[0] -= 1

        def warmup(self):
            pass

        def close(self):
            pass

    shared = SharedDetector(Slow())
    threads = [threading.Thread(target=lambda: [shared.detect(None) for _ in range(5)]) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert peak[0] == 1


def test_health_command(tmp_path, demo_video, capsys):
    cfg_dir = tmp_path
    assert cli.main(["health", "--data-dir", str(cfg_dir / "nothing")]) == 1
    assert "No status file" in capsys.readouterr().out
    status = {"version": "x", "device_id": "edge-1", "updated": time.time(), "detector": {"name": "d"},
              "cameras": [{"camera_id": "door", "state": "running", "fps": 9.8, "frames_processed": 50}]}
    (cfg_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert cli.main(["health", "--data-dir", str(cfg_dir)]) == 0
    assert "door: running, 9.8 FPS" in capsys.readouterr().out
    status["updated"] -= 600
    (cfg_dir / "status.json").write_text(json.dumps(status), encoding="utf-8")
    assert cli.main(["health", "--data-dir", str(cfg_dir)]) == 1
    assert "STALE" in capsys.readouterr().out


def test_run_command_runs_all_cameras(tmp_path, demo_video, capsys):
    import yaml

    cfg = site_config(tmp_path, demo_video)
    path = tmp_path / "site.yaml"
    path.write_text(yaml.safe_dump(cfg.model_dump(mode="json", exclude_none=True)), encoding="utf-8")
    assert cli.main(["run", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "running 2 camera(s): door, till" in out
    assert out.count("Line 'door': IN 4, OUT 2") == 2
    assert cli.main(["run", "--config", str(path), "--source", str(demo_video)]) == 2
    assert "--camera" in capsys.readouterr().err
