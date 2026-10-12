"""Phase 4 B: video analysis jobs (chunked upload, probe, lines, run with the edge engine, results)."""

from __future__ import annotations

import io
import shutil
from datetime import timedelta

import pytest
from conftest import create_org, signup
from pypdf import PdfReader
from sqlalchemy import update
from test_orgs import add_member

from countvision_cloud.db import session_factory, utcnow
from countvision_cloud.product.models import VideoJob
from countvision_cloud.product.services import video_jobs, video_worker
from countvision_cloud.saas.models import Role
from countvision_cloud.settings import get_settings

pytest.importorskip("countvision_edge", reason="the video worker needs the edge package (pip install -e edge)")

DEMO_LINES = {"lines": [{"name": "door", "p1": [0.1, 0.5], "p2": [0.9, 0.5], "in_direction": "to_right"}],
              "zones": [{"name": "waiting", "polygon": [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]],
                         "kind": "queue"}],
              "classes": ["person", "car"], "anchor": "bottom_center", "sample_fps": None, "blur": True}


@pytest.fixture
def demo_video(tmp_path_factory) -> bytes:
    from countvision_edge.testing.synthetic import demo_scene

    path = demo_scene().write_video(tmp_path_factory.mktemp("v") / "demo_scene.avi")
    return path.read_bytes()


@pytest.fixture(autouse=True)
def video_settings(tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "data_dir", str(tmp_path / "data"))
    monkeypatch.setattr(s, "video_runner", "off")  # the test calls probe/run itself
    monkeypatch.setattr(s, "video_detector", "blobs")


def upload(client, org, data: bytes, name="shop door.avi", chunk=200_000) -> dict:
    base = f"/api/orgs/{org['id']}/video-jobs"
    job = client.post(base, json={"name": name, "size_bytes": len(data)}).json()
    for off in range(0, len(data), chunk):
        resp = client.put(f"{base}/{job['id']}/upload", params={"offset": off}, content=data[off:off + chunk],
                          headers={"Content-Type": "application/octet-stream"})
        assert resp.status_code == 200, resp.text
    resp = client.post(f"{base}/{job['id']}/complete")
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_full_video_job(client, client2, demo_video):
    signup(client, "owner@example.com", "Olga")
    org = create_org(client)
    base = f"/api/orgs/{org['id']}/video-jobs"
    job = upload(client, org, demo_video)
    assert job["status"] == "probing" and job["received_bytes"] == len(demo_video)

    video_worker.probe(job["id"])
    job = client.get(f"{base}/{job['id']}").json()
    assert job["status"] == "ready" and job["fps"] == 15 and job["frame_count"] == 270
    assert (job["width"], job["height"]) == (640, 360) and job["duration_s"] == 18
    thumb = client.get(f"{base}/{job['id']}/files/thumbnail.jpg")
    assert thumb.status_code == 200 and thumb.content[:2] == b"\xff\xd8"

    assert client.post(f"{base}/{job['id']}/start").status_code == 422  # no lines yet
    assert client.put(f"{base}/{job['id']}/config", json={**DEMO_LINES, "lines": [], "zones": []}).status_code == 422
    assert client.put(f"{base}/{job['id']}/config", json=DEMO_LINES).status_code == 200
    started = client.post(f"{base}/{job['id']}/start").json()
    assert started["status"] == "queued"

    video_worker.run(job["id"])
    done = client.get(f"{base}/{job['id']}").json()
    assert done["status"] == "done", done["error"]
    r = done["result"]
    door = r["lines"][0]
    assert (door["in"], door["out"]) == (4, 2)  # exactly the demo answer
    assert door["classes"] == {"person": {"in": 3, "out": 2}, "car": {"in": 1, "out": 0}}
    assert r["zones"][0]["name"] == "waiting" and r["zones"][0]["visits"] == 1
    assert 9.5 <= r["zones"][0]["dwell_avg_s"] <= 11.5
    assert r["frames_processed"] == 270 and sum(t["in"] for t in r["timeline"]) == 4
    assert done["input_kept"] is False  # the uploaded video is gone
    files = done["files"]
    assert files["report.pdf"] and files["counts.csv"] and files["thumbnail.jpg"]
    if shutil.which("ffmpeg"):
        assert files["annotated.mp4"] and r["video_codec"] == "h264"
        video = client.get(f"{base}/{job['id']}/files/annotated.mp4")
        assert video.headers["content-type"] == "video/mp4" and video.content[4:8] == b"ftyp"
        part = client.get(f"{base}/{job['id']}/files/annotated.mp4", headers={"Range": "bytes=0-99"})
        assert part.status_code == 206 and len(part.content) == 100  # the browser can seek
    csv_text = client.get(f"{base}/{job['id']}/files/counts.csv").content.decode()
    assert "video_time,line,class,in,out" in csv_text and "0:00:00,door,all,4,2" in csv_text
    pdf_text = PdfReader(io.BytesIO(client.get(f"{base}/{job['id']}/files/report.pdf").content)).pages[0].extract_text()
    assert "shop door.avi" in pdf_text and "Lines" in pdf_text

    # again with the same video is not possible (deleted); viewers read, but cannot start or delete
    assert client.post(f"{base}/{job['id']}/start").json()["error"]["code"] == "input_gone"
    signup(client2, "viewer@example.com", "Vera")
    add_member(org["id"], "viewer@example.com", Role.VIEWER)
    assert client2.get(f"{base}/{job['id']}/files/report.pdf").status_code == 200
    assert client2.delete(f"{base}/{job['id']}").status_code == 403
    assert client.delete(f"{base}/{job['id']}").status_code == 200
    assert not video_jobs.job_dir(done["id"]).exists()


def test_upload_rules(client, demo_video, monkeypatch):
    signup(client, "owner@example.com", "Olga")
    org = create_org(client)
    base = f"/api/orgs/{org['id']}/video-jobs"
    assert client.post(base, json={"name": "notes.txt", "size_bytes": 10}).status_code == 422
    monkeypatch.setattr(get_settings(), "video_max_bytes", 1000)
    assert client.post(base, json={"name": "big.mp4", "size_bytes": 5000}).status_code == 413
    monkeypatch.setattr(get_settings(), "video_max_bytes", 10**9)
    job = client.post(base, json={"name": "a.avi", "size_bytes": len(demo_video)}).json()
    url = f"{base}/{job['id']}/upload"
    assert client.put(url, params={"offset": 0}, content=demo_video[:1000]).status_code == 200
    wrong = client.put(url, params={"offset": 5000}, content=demo_video[5000:6000])
    assert wrong.status_code == 409 and "has 1000 bytes" in wrong.json()["error"]["message"]
    assert client.get(f"{base}/{job['id']}").json()["received_bytes"] == 1000  # resume from here
    assert client.post(f"{base}/{job['id']}/complete").json()["error"]["code"] == "incomplete"
    assert client.put(url, params={"offset": 1000}, content=demo_video[1000:] + b"x").status_code == 422


def test_broken_file_and_cancel_and_cleanup(client, demo_video):
    signup(client, "owner@example.com", "Olga")
    org = create_org(client)
    base = f"/api/orgs/{org['id']}/video-jobs"
    bad = upload(client, org, b"this is not a video" * 100, name="broken.mp4")
    video_worker.probe(bad["id"])
    bad = client.get(f"{base}/{bad['id']}").json()
    assert bad["status"] == "failed" and "cannot be read" in bad["error"]

    job = upload(client, org, demo_video)
    video_worker.probe(job["id"])
    client.put(f"{base}/{job['id']}/config", json=DEMO_LINES)
    client.post(f"{base}/{job['id']}/start")
    assert client.post(f"{base}/{job['id']}/cancel").json()["status"] == "cancelled"
    video_worker.run(job["id"])  # a cancelled job is not run
    assert client.get(f"{base}/{job['id']}").json()["status"] == "cancelled"

    # cleanup: unfinished uploads after a day, the uploaded video of a stopped job after a day,
    # annotated videos after the keep time
    unfinished = client.post(base, json={"name": "x.mp4", "size_bytes": 100}).json()
    now = utcnow()
    with session_factory()() as db:
        db.execute(update(VideoJob).values(updated_at=now - timedelta(days=2)))
        db.commit()
        out = video_jobs.cleanup(db, now)
        db.commit()
    assert out["uploads"] == 1 and out["inputs"] >= 1
    assert client.get(f"{base}/{unfinished['id']}").json()["status"] == "failed"
    assert client.get(f"{base}/{job['id']}").json()["input_kept"] is False


def test_concurrency_limit_and_sampling(client, demo_video, monkeypatch):
    signup(client, "owner@example.com", "Olga")
    org = create_org(client)
    base = f"/api/orgs/{org['id']}/video-jobs"
    monkeypatch.setattr(get_settings(), "video_jobs_per_org", 1)
    a, b = upload(client, org, demo_video), upload(client, org, demo_video, name="b.avi")
    for j in (a, b):
        video_worker.probe(j["id"])
        client.put(f"{base}/{j['id']}/config", json={**DEMO_LINES, "sample_fps": 10})
    assert client.post(f"{base}/{a['id']}/start").status_code == 200
    busy = client.post(f"{base}/{b['id']}/start")
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "busy"
    video_worker.run(a["id"])
    res = client.get(f"{base}/{a['id']}").json()["result"]
    assert res["frames_processed"] < 270  # 10 of 15 frames per second
    assert res["lines"][0]["in"] == 4  # the demo still counts right at 10 FPS
