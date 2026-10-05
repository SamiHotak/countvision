"""Local web app: HTTP API, security rules, saving lines, video upload, exports."""

from __future__ import annotations

import io
import zipfile

import yaml

from countvision_edge.testing.synthetic import DEMO_EXPECTED

WRITE = {"X-CountVision": "1"}


def test_page_and_static_files(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "CountVision" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    meta = client.get("/api/meta").json()
    assert meta["accent"].startswith("#") and "person" in meta["class_colors"]


def test_video_run_gives_exact_counts(client):
    status = client.get("/api/status").json()
    assert status["run"]["kind"] == "file"
    assert status["run"]["state"] == "finished"
    assert status["run"]["camera_id"] == "demo-video"  # a video never mixes with the camera's numbers
    assert status["frame"] == {"width": 640, "height": 360}

    totals = client.get("/api/totals").json()
    assert totals["scope"]["kind"] == "video"
    line = totals["lines"]["entrance"]
    assert line["by_class"]["person"] == {
        "in": DEMO_EXPECTED["person_in"], "out": DEMO_EXPECTED["person_out"]}
    assert line["by_class"]["car"] == {"in": DEMO_EXPECTED["car_in"], "out": DEMO_EXPECTED["car_out"]}
    zone = totals["zones"]["waiting"]
    assert zone["visits"] == 1 and 9.5 <= zone["dwell_avg_s"] <= 11.5


def test_timeline_report_and_exports(client):
    timeline = client.get("/api/timeline").json()
    assert timeline["scope"]["bucket_s"] == 60
    assert sum(b["in"] for b in timeline["buckets"]) == DEMO_EXPECTED["person_in"] + DEMO_EXPECTED["car_in"]
    only = client.get("/api/timeline", params={"line": "nope"}).json()
    assert sum(b["in"] + b["out"] for b in only["buckets"]) == 0

    report = client.get("/api/report").json()
    assert "Line 'entrance': IN 4, OUT 2" in report["text"]
    assert "Zone 'waiting': visits 1" in report["text"]

    csv_response = client.get("/api/export.csv")
    assert csv_response.status_code == 200
    assert "attachment" in csv_response.headers["content-disposition"]
    text = csv_response.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("date,hour,camera,type,name,in,out")
    # Sum the hours: the 18 s demo can run across a full hour (the test was flaky at hh:59).
    assert _sum_line(text, ",", "entrance") == (4, 2)

    semicolon = client.get("/api/export.csv", params={"delimiter": ";"}).content.decode("utf-8-sig")
    assert _sum_line(semicolon, ";", "entrance") == (4, 2)
    assert client.get("/api/export.csv", params={"delimiter": "|"}).status_code == 422

    archive = zipfile.ZipFile(io.BytesIO(client.get("/api/export.zip").content))
    assert {"hourly.csv", "events.csv", "line_counts.csv", "zone_stats.csv", "coverage.csv"} <= set(
        archive.namelist()
    )


def test_writes_need_the_header(client):
    body = {"lines": [], "zones": []}
    assert client.put("/api/geometry", json=body).status_code == 403
    assert client.post("/api/stop").status_code == 403


def test_unknown_host_is_rejected(client):
    assert client.get("/api/status", headers={"host": "evil.example"}).status_code == 400


def test_access_key_when_open_to_the_network(runner):
    from fastapi.testclient import TestClient

    from countvision_edge.app import create_app

    app = create_app(runner, access_key="secret-key", autostart=False)
    with TestClient(app) as test_client:
        assert test_client.get("/api/status").status_code == 401
        assert test_client.get("/api/status", params={"key": "wrong"}).status_code == 401
        assert test_client.get("/", params={"key": "secret-key"}).status_code == 200
        assert test_client.get("/api/status").status_code == 200  # the cookie is kept


def test_save_geometry_keeps_comments_and_settings(client, app_config):
    geometry = client.get("/api/geometry").json()
    assert geometry["ready"] and geometry["lines"][0]["name"] == "entrance"
    lines = [
        {"name": "entrance", "p1": [0.1, 0.45], "p2": [0.9, 0.45], "in_direction": "to_left"},
        {"name": "side door", "p1": [0.95, 0.1], "p2": [0.95, 0.9], "in_direction": "to_right"},
    ]
    zones = [{"name": "waiting", "kind": "area", "polygon": [[0.05, 0.6], [0.5, 0.6], [0.5, 0.95]]}]
    result = client.put("/api/geometry", json={"lines": lines, "zones": zones}, headers=WRITE)
    assert result.status_code == 200, result.text
    assert [line["name"] for line in result.json()["lines"]] == ["entrance", "side door"]

    text = app_config.read_text(encoding="utf-8")
    assert "# Test config. This comment must survive" in text
    assert "# demo detector, no model" in text
    saved = yaml.safe_load(text)["cameras"][0]
    assert saved["lines"][0]["in_direction"] == "to_left"
    assert saved["lines"][0]["deadband_px"] == 3  # setting not shown in the page is kept
    assert saved["lines"][1]["name"] == "side door"
    assert saved["zones"][0]["kind"] == "area" and len(saved["zones"][0]["polygon"]) == 3
    assert app_config.with_name("config.yaml.bak").is_file()
    assert client.get("/api/status").json()["config_version"] == 1


def test_bad_geometry_is_rejected_with_a_message(client, app_config):
    before = app_config.read_text(encoding="utf-8")
    same = {"name": "a", "p1": [0.2, 0.2], "p2": [0.2, 0.2]}
    response = client.put("/api/geometry", json={"lines": [same], "zones": []}, headers=WRITE)
    assert response.status_code == 422
    assert "different points" in response.json()["detail"]
    twins = [{"name": "a", "p1": [0, 0], "p2": [1, 1]}, {"name": "a", "p1": [0, 1], "p2": [1, 0]}]
    response = client.put("/api/geometry", json={"lines": twins, "zones": []}, headers=WRITE)
    assert response.status_code == 422 and "unique" in response.json()["detail"]
    flat = {"name": "z", "polygon": [[0.1, 0.1], [0.2, 0.2], [0.3, 0.3]]}
    response = client.put("/api/geometry", json={"lines": [], "zones": [flat]}, headers=WRITE)
    assert response.status_code == 422 and "no area" in response.json()["detail"]
    outside = {"name": "o", "p1": [0.1, 0.1], "p2": [3.0, 0.1]}
    assert client.put("/api/geometry", json={"lines": [outside], "zones": []}, headers=WRITE).status_code == 422
    assert app_config.read_text(encoding="utf-8") == before  # nothing written


def test_new_line_counts_in_the_next_run(client, runner):
    vertical = [{"name": "middle", "p1": [0.5, 0.0], "p2": [0.5, 1.0], "in_direction": "to_right"}]
    assert client.put("/api/geometry", json={"lines": vertical, "zones": []}, headers=WRITE).status_code == 200
    assert client.post("/api/source", json={"kind": "config"}, headers=WRITE).status_code == 200
    assert runner.wait(60)
    totals = client.get("/api/totals").json()
    assert set(totals["lines"]) == {"middle"}  # the new run uses only the new line
    assert totals["zones"] == {}  # the old "entrance" counts of the first run are not mixed in


def test_upload_counts_and_deletes_the_video(client, runner, demo_video, tmp_path):
    with open(demo_video, "rb") as handle:
        response = client.post(
            "/api/upload", files={"file": ("shop door.avi", handle, "video/x-msvideo")}, headers=WRITE
        )
    assert response.status_code == 200, response.text
    assert runner.wait(60)
    status = client.get("/api/status").json()
    assert status["run"]["label"] == "shop door.avi" and status["run"]["state"] == "finished"
    assert list((tmp_path / "data" / "uploads").iterdir()) == []  # the video was deleted
    assert client.get("/api/totals").json()["lines"]["entrance"]["in"] == 4


def test_upload_rejects_other_files(client):
    response = client.post(
        "/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")}, headers=WRITE
    )
    assert response.status_code == 415


def test_preview_jpeg_while_running(app_config, runner):
    from fastapi.testclient import TestClient

    from countvision_edge.app import create_app
    from countvision_edge.config import SourceConfig

    app = create_app(runner, trusted_hosts={"testserver"}, autostart=False)
    with TestClient(app) as test_client:
        runner.touch_viewer()
        source = runner.base_cam.source.model_copy(update={"realtime": True})
        runner.start(SourceConfig.model_validate(source.model_dump()))
        response = test_client.get("/api/preview.jpg")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"
        assert response.content[:2] == b"\xff\xd8"
        assert test_client.post("/api/stop", headers=WRITE).json()["run"]["state"] == "stopped"


def _sum_line(text: str, sep: str, name: str) -> tuple[int, int]:
    """IN and OUT of one line, summed over all hourly rows of the CSV export."""
    rows = [r.split(sep) for r in text.splitlines()[1:]]
    hits = [r for r in rows if len(r) > 6 and r[3] == "line" and r[4] == name]
    assert hits, text
    return sum(int(r[5]) for r in hits), sum(int(r[6]) for r in hits)
