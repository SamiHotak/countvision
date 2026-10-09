"""Cloud connection: pairing, uploads, offline buffer replay, revoked devices.

A small fake cloud (http.server in a thread) speaks the same JSON as the real API, so the
real urllib code is tested without the cloud package.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from countvision_edge import cli
from countvision_edge.cloud import CloudError, CloudUploader, Credentials, normalize_url, pair
from countvision_edge.inputs.live_source import Backoff
from countvision_edge.storage import CoverageRow, LineCountRow, SqliteBuffer
from countvision_edge.types import Event

from .test_supervisor import site_config


class FakeCloud:
    """Stores what it receives like the real cloud: minute rows overwrite, events once."""

    def __init__(self) -> None:
        self.online = True
        self.revoked = False
        self.lines: dict[tuple, tuple[int, int]] = {}
        self.events: set[str] = set()
        self.batches: list[dict] = []
        self.code = "K7QF-3MXP"
        self.token = "cvd_test-token"
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # quiet
                pass

            def _json(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/api/device/me":
                    if self.headers.get("Authorization") != f"Bearer {cloud.token}":
                        return self._json(401, {"error": {"code": "device_token_invalid", "message": "Invalid device token."}})
                    return self._json(200, {"name": "Mini PC", "site": "Shop"})
                self._json(404, {"error": {"code": "not_found", "message": "nope"}})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/device/pair":
                    if body["code"].replace("-", "").upper() != cloud.code.replace("-", ""):
                        return self._json(400, {"error": {"code": "invalid_code", "message": "This pairing code is wrong."}})
                    return self._json(200, {"device_id": "dev-1", "token": cloud.token, "org_name": "Café Sonne",
                                            "site_name": "Shop", "device_name": "Mini PC"})
                if self.path == "/api/device/ingest":
                    if cloud.revoked or self.headers.get("Authorization") != f"Bearer {cloud.token}":
                        return self._json(401, {"error": {"code": "device_revoked", "message": "This device was removed."}})
                    cloud.batches.append(body)
                    for r in body["line_counts"]:
                        cloud.lines[(r["camera_id"], r["window_start"], r["line"], r["class_name"])] = (
                            r["in_count"], r["out_count"])
                    cloud.events.update(e["event_id"] for e in body["events"])
                    return self._json(200, {"accepted": {}, "rejected": 0, "server_time": time.time(),
                                            "duplicate_batch": False})
                self._json(404, {"error": {"code": "not_found", "message": "nope"}})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def totals(self, line: str = "door", cls: str = "*") -> tuple[int, int]:
        rows = [v for k, v in self.lines.items() if k[2] == line and k[3] == cls]
        return sum(r[0] for r in rows), sum(r[1] for r in rows)

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def cloud():
    fake = FakeCloud()
    yield fake
    fake.close()


def creds(cloud: FakeCloud) -> Credentials:
    return Credentials(url=cloud.url, device_id="dev-1", token=cloud.token, organization="Café Sonne",
                       site="Shop", device_name="Mini PC")


def line(minute: int, n_in: int, n_out: int = 0, cam: str = "door") -> LineCountRow:
    return LineCountRow(cam, minute, "door", "*", n_in, n_out)


# -------------------------------------------------------------------- storage helpers


def test_mark_sent_rows_keeps_rows_that_changed_during_upload(tmp_path):
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    buf.add_line_counts([line(600, 2)])
    fetched = buf.fetch_unsent("line_counts")
    buf.add_line_counts([line(600, 1)])  # late update while the upload is on the way
    assert buf.mark_sent_rows("line_counts", fetched) == 0
    again = buf.fetch_unsent("line_counts")
    assert again[0]["in_count"] == 3
    assert buf.mark_sent_rows("line_counts", again) == 1
    assert buf.count_unsent() == 0
    buf.close()


# ------------------------------------------------------------------------ pairing


def test_pair_saves_private_credentials(tmp_path, cloud):
    c = pair(cloud.url + "/", "k7qf3mxp", tmp_path, "edge-abc")
    assert c.organization == "Café Sonne" and c.token == cloud.token and c.url == cloud.url
    saved = Credentials.load(tmp_path)
    assert saved == c
    import os
    import stat

    if os.name == "posix":
        assert stat.S_IMODE(os.stat(Credentials.path(tmp_path)).st_mode) == 0o600
    with pytest.raises(CloudError, match="wrong"):
        pair(cloud.url, "AAAA-AAAA", tmp_path / "other", "edge-abc")


def test_cli_pair_status_unpair(tmp_path, cloud, capsys):
    data = str(tmp_path / "data")
    assert cli.main(["pair", "--url", cloud.url, "--code", cloud.code, "--data-dir", data]) == 0
    assert "Organization: Café Sonne" in capsys.readouterr().out
    assert cli.main(["pair", "--url", cloud.url, "--code", cloud.code, "--data-dir", data]) == 1  # needs --force
    assert cli.main(["cloud-status", "--data-dir", data]) == 0
    assert "Connection OK" in capsys.readouterr().out
    cloud.token = "cvd_rotated"  # the cloud no longer knows this token
    assert cli.main(["cloud-status", "--data-dir", data]) == 2
    assert cli.main(["unpair", "--data-dir", data]) == 0
    assert Credentials.load(data) is None
    assert cli.main(["pair", "--url", "localhost:3000", "--code", "x", "--data-dir", data]) == 2  # no http://


def test_normalize_url():
    assert normalize_url("https://app.example.com/api/") == "https://app.example.com"
    with pytest.raises(Exception, match="http"):
        normalize_url("app.example.com")


# ------------------------------------------------------------------------- uploads


def test_upload_sends_everything_once_and_only_newest_heartbeat(tmp_path, cloud):
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    buf.add_line_counts([line(600, 2, 1), line(660, 1)])
    buf.add_coverage(CoverageRow("door", 600, 600, 60.0))
    buf.add_events([Event("e1", "line_cross", 610.0, "door", "door", 3, "person", "in")])
    for i in range(5):
        buf.add_heartbeat({"ts": 1000.0 + i, "camera_id": "door", "fps": 9.0 + i, "connected": 1})
    up = CloudUploader(buf, creds(cloud), status_fn=lambda: {"version": "x", "cameras": [{"id": "door"}]})
    assert up.upload_once() is False
    batch = cloud.batches[0]
    assert [h["fps"] for h in batch["heartbeats"]] == [13.0]  # newest only
    assert "id" not in batch["line_counts"][0] and "sent" not in batch["line_counts"][0]
    assert batch["cameras"] == [{"id": "door"}] and batch["status"]["upload"]["state"] == "starting"
    assert cloud.totals() == (3, 1) and cloud.events == {"e1"}
    assert buf.count_unsent() == 0 and buf.fetch_unsent("heartbeats") == []
    up.upload_once()  # nothing new: an empty batch = "I am alive"
    assert cloud.batches[1]["line_counts"] == [] and cloud.totals() == (3, 1)
    buf.close()


def test_network_outage_loses_nothing(tmp_path, cloud):
    """Unplug the network: counting goes on, the backlog is sent when it is back."""
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    up = CloudUploader(buf, creds(cloud), interval_s=0.05, batch_rows=10)
    up._backoff = Backoff(0.05, 1.0, 0.05)  # fast retries for the test
    real_send = up._send

    def send(payload):
        if not cloud.online:
            raise CloudError("cannot reach the cloud (connection refused)")
        return real_send(payload)

    up._send = send
    cloud.online = False
    up.start()
    for m in range(25):  # 25 minutes of counts while offline (more than one batch)
        buf.add_line_counts([line(6000 + 60 * m, 1)])
    time.sleep(0.4)
    assert up.state.state == "offline" and cloud.totals() == (0, 0)
    assert "connection refused" in up.state.last_error
    cloud.online = True
    deadline = time.time() + 5
    while (cloud.totals() != (25, 0) or up.state.state != "ok") and time.time() < deadline:
        time.sleep(0.05)
    up.stop()
    up.join(2)
    assert cloud.totals() == (25, 0) and buf.count_unsent() == 0 and up.state.state == "ok"
    assert len([b for b in cloud.batches if b["line_counts"]]) >= 3  # sent in several batches
    buf.close()


def test_revoked_device_stops_uploading_but_keeps_data(tmp_path, cloud):
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    buf.add_line_counts([line(600, 4)])
    cloud.revoked = True
    up = CloudUploader(buf, creds(cloud), interval_s=0.05)
    up.start()
    up.join(3)
    assert not up.is_alive() and up.state.state == "revoked"
    assert buf.count_unsent() == 1  # still here
    buf.close()


def test_supervisor_uploads_the_demo_counts(tmp_path, demo_video, cloud):
    """The whole agent: two file cameras count the demo scene and upload the result."""
    from countvision_edge.detectors import build_detector
    from countvision_edge.supervisor import Supervisor

    cfg = site_config(tmp_path, demo_video)
    creds(cloud).save(cfg.data_dir)
    cfg.cloud.upload_interval_s = 2
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    sup = Supervisor(cfg, build_detector(cfg.detector), buf)
    sup.run_forever()  # files end -> final upload on shutdown
    assert sup.uploader is not None
    for cam in ("door", "till"):
        rows = {k: v for k, v in cloud.lines.items() if k[0] == cam and k[3] == "*"}
        assert sum(v[0] for v in rows.values()) == 4 and sum(v[1] for v in rows.values()) == 2
    statuses = [b["cameras"] for b in cloud.batches if b["cameras"]]
    assert statuses and statuses[-1][0]["lines"] == ["door"]
    assert "pid" not in cloud.batches[-1]["status"]
    status = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["upload"]["state"] == "ok" and status["upload"]["pending"] == 0
    buf.close()


def test_not_paired_means_local_only(tmp_path, demo_video):
    from countvision_edge.detectors import build_detector
    from countvision_edge.supervisor import Supervisor

    cfg = site_config(tmp_path, demo_video, cameras=("door",))
    buf = SqliteBuffer(tmp_path / "db.sqlite")
    sup = Supervisor(cfg, build_detector(cfg.detector), buf)
    sup.run_forever()
    assert sup.uploader is None
    status = json.loads((tmp_path / "data" / "status.json").read_text(encoding="utf-8"))
    assert status["upload"] == {"state": "disabled"}
    buf.close()
