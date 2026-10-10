"""Config from the cloud (Phase 3 C): schedules, applying lines/zones to a running camera,
the saved overlay, snapshots (pixelated, on request only) and fast event uploads."""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from zoneinfo import ZoneInfo

import cv2
import numpy as np
import pytest

from countvision_edge.cloud import CloudError, CloudUploader
from countvision_edge.cloud_sync import ConfigSync, apply_doc, load_overlay, report_doc
from countvision_edge.config import ScheduleConfig
from countvision_edge.detectors import build_detector
from countvision_edge.storage import SqliteBuffer
from countvision_edge.supervisor import Supervisor, check_health
from countvision_edge.types import Event

from .test_cloud import FakeCloud, creds
from .test_supervisor import FakeLive, site_config

BERLIN = ZoneInfo("Europe/Berlin")


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=BERLIN).timestamp()


DOC = {
    "lines": [{"name": "entrance", "p1": [0.5, 0.1], "p2": [0.5, 0.9], "in_direction": "to_left"}],
    "zones": [{"name": "till", "polygon": [[0.1, 0.1], [0.4, 0.1], [0.4, 0.4]], "kind": "queue"}],
    "classes": ["person"],
    "anchor": "center",
    "schedule": {"days": [0, 1, 2, 3, 4], "start": "08:00", "end": "18:30"},
}


# --------------------------------------------------------------------------- schedule


def test_schedule_weekdays_and_hours():
    s = ScheduleConfig(days=[0, 1, 2, 3, 4], start="08:00", end="18:30", timezone="Europe/Berlin")
    assert s.is_active(ts("2026-10-07T08:00:00"))  # Wednesday
    assert s.is_active(ts("2026-10-07T18:30:59"))
    assert not s.is_active(ts("2026-10-07T07:59:00"))
    assert not s.is_active(ts("2026-10-07T18:31:00"))
    assert not s.is_active(ts("2026-10-10T12:00:00"))  # Saturday


def test_schedule_over_midnight_belongs_to_the_day_it_starts():
    s = ScheduleConfig(days=[4], start="22:00", end="03:00", timezone="Europe/Berlin")  # Friday night
    assert s.is_active(ts("2026-10-09T23:00:00"))  # Friday
    assert s.is_active(ts("2026-10-10T02:59:00"))  # Saturday early = still Friday's night
    assert not s.is_active(ts("2026-10-10T23:00:00"))  # Saturday night
    assert not s.is_active(ts("2026-10-09T02:00:00"))  # Friday early = Thursday's night


def test_schedule_uses_the_site_time_zone():
    utc_noon = datetime(2026, 10, 7, 12, 0, tzinfo=ZoneInfo("UTC")).timestamp()
    assert ScheduleConfig(start="13:30", end="14:30", timezone="Europe/Berlin").is_active(utc_noon)
    assert not ScheduleConfig(start="13:30", end="14:30", timezone="America/New_York").is_active(utc_noon)


# ----------------------------------------------------------------------- conversion


def test_apply_and_report_round_trip(tmp_path, demo_video):
    cam = site_config(tmp_path, demo_video, cameras=("door",)).cameras[0]
    new = apply_doc(cam, DOC, "Europe/Berlin")
    assert [line.name for line in new.lines] == ["entrance"] and new.lines[0].in_direction == "to_left"
    assert new.zones[0].kind == "queue" and new.classes == ["person"] and new.anchor == "center"
    assert new.schedule.timezone == "Europe/Berlin" and new.source == cam.source
    assert report_doc(new, (640, 360)) == DOC


def test_pixel_config_is_converted_to_normalized(tmp_path, demo_video):
    cfg = site_config(tmp_path, demo_video, cameras=("door",))
    data = cfg.cameras[0].model_dump()
    data.update(coordinates="pixel", lines=[{"name": "door", "p1": [64, 180], "p2": [576, 180]}])
    cam = type(cfg.cameras[0]).model_validate(data)
    assert report_doc(cam, None) is None  # no picture yet: size unknown
    assert report_doc(cam, (640, 360))["lines"][0]["p1"] == [0.1, 0.5]
    new = apply_doc(cam, DOC, None, (640, 360))
    assert new.coordinates == "normalized" and new.schedule.timezone is None


def test_invalid_doc_is_refused(tmp_path, demo_video):
    cam = site_config(tmp_path, demo_video, cameras=("door",)).cameras[0]
    bad = dict(DOC, zones=[{"name": "x", "polygon": [[0, 0], [1, 1]]}])  # 2 points
    with pytest.raises(ValueError):
        apply_doc(cam, bad, None)


# ------------------------------------------------------------------- ConfigSync thread


def test_config_sync_applies_each_version_once_and_answers_snapshots():
    answers = [
        {"rev": 3, "changed": True, "cameras": [{"id": "door", "version": 2, "config": DOC, "timezone": "UTC"}],
         "snapshots": [{"request_id": "r1", "camera_id": "door"}, {"request_id": "r2", "camera_id": "nope"}]},
        {"rev": 3, "changed": False},
        {"rev": 4, "changed": True, "cameras": [{"id": "door", "version": 2, "config": DOC}], "snapshots": []},
    ]
    calls, applied, sent = [], [], []

    def http(method, url, *a, **kw):
        calls.append(url)
        return answers.pop(0)

    def snap(cam):
        if cam != "door":
            raise LookupError("camera nope is not configured on this device")
        return b"\xff\xd8jpeg"

    sync = ConfigSync(creds_for("http://cloud"), apply_config=lambda *args: applied.append(args),
                      take_snapshot=snap, http=http, send_snapshot=lambda *args: sent.append(args))
    assert sync.poll_once() is True
    assert applied == [("door", 2, DOC, "UTC")]
    assert sent == [("r1", b"\xff\xd8jpeg", None), ("r2", None, "camera nope is not configured on this device")]
    assert sync.poll_once() is False
    assert sync.poll_once() is True and len(applied) == 1  # same version: not applied again
    assert "rev=-1" in calls[0] and "rev=3" in calls[1] and "wait=25" in calls[0]


def test_config_sync_stops_when_the_device_is_removed():
    def http(*a, **kw):
        raise CloudError("This device was removed.", status=401)

    sync = ConfigSync(creds_for("http://cloud"), apply_config=lambda *a: None, take_snapshot=lambda c: b"",
                      http=http)
    sync.run()  # returns at once
    assert sync.state == "revoked"


def creds_for(url: str):
    from countvision_edge.cloud import Credentials

    return Credentials(url=url, device_id="dev-1", token="cvd_x", organization="O", site="S", device_name="D")


class PollCloud:
    """A fake cloud with the long-poll and the snapshot upload (real HTTP for urllib)."""

    def __init__(self) -> None:
        self.state = {"rev": 0, "cameras": [], "snapshots": []}
        self.images: dict[str, bytes | dict] = {}
        self.changed = threading.Event()
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                rev = int(self.path.split("rev=")[1].split("&")[0])
                deadline = time.time() + 2
                while cloud.state["rev"] == rev and time.time() < deadline:
                    cloud.changed.wait(0.05)
                body = {"rev": cloud.state["rev"], "changed": cloud.state["rev"] != rev}
                if body["changed"]:
                    body.update(cameras=cloud.state["cameras"], snapshots=cloud.state["snapshots"])
                    cloud.state["snapshots"] = []
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                req = self.path.rsplit("/", 1)[1]
                ctype = self.headers["Content-Type"]
                cloud.images[req] = raw if ctype == "image/jpeg" else json.loads(raw)
                self.send_response(204)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def push(self, **changes) -> None:
        self.state.update(changes)
        self.state["rev"] += 1
        self.changed.set()
        self.changed.clear()


def wait_for(check, timeout: float = 8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


def live_config(tmp_path, demo_video, **extra):
    cfg = site_config(tmp_path, demo_video, cameras=("door",), **extra)
    cfg.cameras[0].source.kind = "rtsp"
    return cfg


CHECKER = (np.indices((40, 40)).sum(axis=0) // 2 % 2).astype(bool)  # 2-px checkerboard


class BrightLive(FakeLive):
    """FakeLive with a red checkered square (the blob detector sees a 'person' there) and a
    grey checkered square (not detected) to compare with."""

    def read(self, timeout: float = 1.0):
        frame = super().read(timeout)
        if frame is not None:
            img = frame.image
            img[:] = 90
            person = np.zeros((40, 40, 3), np.uint8)
            person[:] = (0, 0, 255)
            person[CHECKER] = (0, 0, 150)
            img[30:70, 20:60] = person
            grey = np.full((40, 40, 3), 60, np.uint8)
            grey[CHECKER] = 200
            img[30:70, 100:140] = grey
        return frame


def test_line_drawn_in_the_cloud_is_used_by_the_running_camera(tmp_path, demo_video):
    """The Phase 3 'done when': a config from the cloud reaches the running camera in seconds,
    is saved, and is still used after a restart."""
    cfg = live_config(tmp_path, demo_video)
    poll = PollCloud()
    creds_for(poll.url).save(cfg.data_dir)
    buffer = SqliteBuffer(":memory:")
    fake = BrightLive()
    sup = Supervisor(cfg, build_detector(cfg.detector), buffer, source_factory=lambda cam: fake)
    sup.start()
    sync = ConfigSync(creds_for(poll.url), apply_config=sup.apply_camera_config, take_snapshot=sup.snapshot,
                      wait_s=2)
    sup.start_config_sync(sync)
    try:
        worker = sup.workers[0]
        assert wait_for(lambda: worker.frame_size == (160, 90))
        assert report_doc(worker.cam, worker.frame_size)["lines"][0]["name"] == "door"
        started = time.time()
        poll.push(cameras=[{"id": "door", "version": 1, "config": DOC, "timezone": "Europe/Berlin"}])
        assert wait_for(lambda: worker.pipeline.cam.lines[0].name == "entrance")
        assert time.time() - started < 3  # within seconds
        assert worker.health.config_version == 1 and worker.health.config_error is None
        status = sup.cloud_status()["cameras"][0]
        assert status["config_version"] == 1 and status["config"]["lines"][0]["name"] == "entrance"
        assert status["frame"] == [160, 90] and status["snapshots_allowed"] is True
        assert load_overlay(cfg.data_dir)["door"]["version"] == 1

        # a class the model does not know: refused, the old config keeps counting
        bad = dict(DOC, classes=["unicorn"])
        poll.push(cameras=[{"id": "door", "version": 2, "config": bad, "timezone": None}])
        assert wait_for(lambda: worker.health.config_error is not None)
        assert "unicorn" in worker.health.config_error and worker.health.config_version == 1
        assert worker.pipeline.cam.lines[0].name == "entrance"

        # snapshot on request: one JPEG, the bright 'person' pixelated
        poll.push(snapshots=[{"request_id": "abc", "camera_id": "door"}])
        assert wait_for(lambda: "abc" in poll.images)
        jpeg = poll.images["abc"]
        assert isinstance(jpeg, bytes) and jpeg[:2] == b"\xff\xd8"
        image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE)
        assert image.shape == (90, 160)
        sup.write_status()
        assert "cloud config v1" in "\n".join(check_health(sup.status_path).lines)
    finally:
        sync.stop()
        sup.stop()
        sup.join(5)
    # restart: the saved cloud config is used, not the YAML one
    again = Supervisor(cfg, build_detector(cfg.detector), buffer, source_factory=lambda cam: BrightLive())
    assert again.workers[0].cam.lines[0].name == "entrance" and again.workers[0].health.config_version == 1
    buffer.close()


def test_snapshot_pixelates_people(tmp_path, demo_video):
    cfg = live_config(tmp_path, demo_video)
    fake = BrightLive()
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"), source_factory=lambda cam: fake)
    sup.start()
    try:
        assert wait_for(lambda: sup.workers[0].refresh().state == "running"
                        and sup.workers[0].last_result is not None)
        jpeg = sup.snapshot("door")
    finally:
        sup.stop()
        sup.join(5)
    image = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_GRAYSCALE).astype(float)
    person, grey = image[36:64, 26:54], image[36:64, 106:134]
    # the 'person' was pixelated (fine pattern gone), the undetected grey pattern was not
    assert grey.std() > 30
    assert person.std() < grey.std() / 3
    with pytest.raises(LookupError):
        sup.snapshot("other")


def test_snapshots_can_be_switched_off(tmp_path, demo_video):
    cfg = live_config(tmp_path, demo_video, privacy={"snapshots": False})
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"),
                     source_factory=lambda cam: FakeLive())
    with pytest.raises(PermissionError, match="switched off"):
        sup.snapshot("door")
    assert sup.cloud_status()["cameras"][0]["snapshots_allowed"] is False


def test_paused_outside_the_schedule(tmp_path, demo_video):
    now = datetime.now(BERLIN)
    other_day = (now.weekday() + 3) % 7
    cfg = live_config(tmp_path, demo_video)
    cfg.cameras[0] = cfg.cameras[0].model_copy(update={"schedule": ScheduleConfig(
        days=[other_day], timezone="Europe/Berlin")})
    sup = Supervisor(cfg, build_detector(cfg.detector), SqliteBuffer(":memory:"),
                     source_factory=lambda cam: BrightLive())
    sup.start()
    try:
        assert wait_for(lambda: sup.workers[0].refresh().paused)
        time.sleep(0.3)
        assert sup.workers[0].refresh().frames_processed == 0
        assert sup.snapshot("door")[:2] == b"\xff\xd8"  # a snapshot still works (for drawing)
        # the cloud removes the schedule: counting starts
        sup.apply_camera_config("door", 5, dict(DOC, schedule=None), "Europe/Berlin")
        assert wait_for(lambda: sup.workers[0].refresh().frames_processed > 0)
        assert not sup.workers[0].health.paused
    finally:
        sup.stop()
        sup.join(5)


def test_new_events_are_uploaded_within_seconds(tmp_path):
    """Live counters: a line crossing goes up at once, not after the 15 s interval."""
    cloud = FakeCloud()
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    up = CloudUploader(buf, creds(cloud), url=cloud.url, interval_s=30)
    up.start()
    try:
        assert wait_for(lambda: up.state.state == "ok")
        first = len(cloud.batches)
        buf.add_events([Event(event_id="e1", kind="line_cross", ts=time.time(), camera_id="door", name="door",
                              track_id=1, class_name="person", direction="in")])
        started = time.time()
        assert wait_for(lambda: "e1" in cloud.events, timeout=5)
        assert time.time() - started < 2.5 and len(cloud.batches) == first + 1
        up.wake()  # wake() = upload now (used after a new config)
        assert wait_for(lambda: len(cloud.batches) == first + 2, timeout=3)
    finally:
        up.stop()
        up.join(3)
        cloud.close()
        buf.close()
