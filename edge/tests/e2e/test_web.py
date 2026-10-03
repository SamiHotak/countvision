"""Browser tests of the main flows in the local web app (Playwright, Chromium).

Run:   pip install -e "edge[dev,e2e]"
       python -m playwright install chromium
       python -m pytest edge/tests/e2e -m e2e

The tests start the real server on a free port with the synthetic demo video (no camera,
no model). They are skipped when Playwright or its browser is not installed. To use a
browser that is already on the computer, set CV_CHROMIUM to its executable.
"""

from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.e2e
sync_api = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def server(tmp_path: Path, demo_video: str):
    """The real app (uvicorn) on the demo video, playing at normal speed and looping."""
    import uvicorn

    from countvision_edge.app import LiveRunner, create_app
    from countvision_edge.config import load_config
    from countvision_edge.detectors import build_detector
    from countvision_edge.storage import SqliteBuffer

    video = tmp_path / "demo.avi"
    shutil.copy(demo_video, video)
    config = tmp_path / "local.yaml"
    config.write_text(yaml.safe_dump({
        "data_dir": (tmp_path / "data").as_posix(),
        "detector": {"type": "blobs"},
        "cameras": [{
            "id": "demo", "name": "Demo scene", "classes": ["person", "car"],
            "source": {"uri": video.as_posix(), "realtime": True},
            "lines": [{"name": "entrance", "p1": [0.1, 0.5], "p2": [0.9, 0.5]}],
            "zones": [{"name": "waiting", "kind": "queue",
                       "polygon": [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]]}],
        }],
    }), encoding="utf-8")
    cfg = load_config(config)
    buffer = SqliteBuffer(cfg.db_file())
    runner = LiveRunner(cfg, cfg.camera(), build_detector(cfg.detector), buffer, config_path=config)
    port = _free_port()
    app = create_app(runner, trusted_hosts={"127.0.0.1", "localhost"})
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                        timeout_graceful_shutdown=1))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    deadline = time.time() + 15
    while not srv.started and time.time() < deadline:
        time.sleep(0.05)
    assert srv.started, "server did not start"
    yield {"url": f"http://127.0.0.1:{port}/", "config": config, "runner": runner}
    srv.should_exit = True
    thread.join(10)
    buffer.close()


@pytest.fixture
def browser():
    with sync_api.sync_playwright() as p:
        try:
            launched = p.chromium.launch(executable_path=os.environ.get("CV_CHROMIUM") or None)
        except Exception as exc:  # noqa: BLE001 - browser not installed
            pytest.skip(f"Chromium for Playwright is not available: {exc}")
        yield launched
        launched.close()


def _drag(page, start, end, steps=12):
    box = page.locator("#overlay").bounding_box()
    to = lambda p: (box["x"] + box["width"] * p[0], box["y"] + box["height"] * p[1])  # noqa: E731
    page.mouse.move(*to(start))
    page.mouse.down()
    page.mouse.move(*to(end), steps=steps)
    page.mouse.up()


def test_live_counters_and_preview(server, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(server["url"])
    sync_api.expect(page.locator("#lineList [data-name='entrance']")).to_be_visible(timeout=10000)
    sync_api.expect(page.locator("#stage")).to_have_class("stage has-picture", timeout=10000)
    sync_api.expect(page.locator("#statusText")).to_contain_text("Counting video", timeout=10000)
    # the first person crosses the line after a few seconds of video
    sync_api.expect(page.locator("#lineList [data-k='in']")).not_to_have_text("0", timeout=20000)
    assert page.locator("#chart rect.bin").count() >= 1
    assert errors == []


def test_draw_line_save_undo_and_flip(server, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(server["url"])
    sync_api.expect(page.locator("#lineList [data-name='entrance']")).to_be_visible(timeout=10000)

    page.click("#btnEdit")
    sync_api.expect(page.locator("#editBar")).to_be_visible()
    page.click("[data-tool='line']")
    _drag(page, (0.2, 0.2), (0.6, 0.3))
    sync_api.expect(page.locator("#shapeList")).to_contain_text("line 1")
    sync_api.expect(page.locator("#propName")).to_have_value("line 1")

    # flip, then undo the flip
    page.keyboard.press("f")
    sync_api.expect(page.locator("#shapeList")).to_contain_text("line, flipped")
    page.click("#btnUndo")
    sync_api.expect(page.locator("#shapeList")).not_to_contain_text("flipped")

    page.fill("#propName", "back door")
    page.click("#btnSave")
    sync_api.expect(page.locator("#toast")).to_contain_text("Saved", timeout=5000)
    sync_api.expect(page.locator("#lineList [data-name='back door']")).to_be_visible()

    saved = yaml.safe_load(server["config"].read_text(encoding="utf-8"))["cameras"][0]["lines"]
    names = [line["name"] for line in saved]
    assert names == ["entrance", "back door"]
    p1, p2 = saved[1]["p1"], saved[1]["p2"]
    assert abs(p1[0] - 0.2) < 0.02 and abs(p1[1] - 0.2) < 0.02
    assert abs(p2[0] - 0.6) < 0.02 and abs(p2[1] - 0.3) < 0.02
    assert saved[1]["in_direction"] == "to_right"


def test_draw_zone_and_move_corner(server, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(server["url"])
    sync_api.expect(page.locator("#zoneList [data-name='waiting']")).to_be_visible(timeout=10000)
    page.click("#btnEdit")
    page.click("[data-tool='zone']")
    box = page.locator("#overlay").bounding_box()
    for x, y in ((0.6, 0.1), (0.9, 0.1), (0.9, 0.4), (0.6, 0.4)):
        page.mouse.click(box["x"] + box["width"] * x, box["y"] + box["height"] * y)
    page.keyboard.press("Enter")
    sync_api.expect(page.locator("#shapeList")).to_contain_text("zone 1")
    page.select_option("#propKind", "queue")
    sync_api.expect(page.locator("#shapeList li").nth(2)).to_contain_text("queue")
    # drag the first corner of the new zone
    _drag(page, (0.6, 0.1), (0.55, 0.05))
    page.click("#btnSave")
    sync_api.expect(page.locator("#toast")).to_contain_text("Saved", timeout=5000)
    zones = yaml.safe_load(server["config"].read_text(encoding="utf-8"))["cameras"][0]["zones"]
    new = zones[1]
    assert new["name"] == "zone 1" and new["kind"] == "queue" and len(new["polygon"]) == 4
    assert abs(new["polygon"][0][0] - 0.55) < 0.02 and abs(new["polygon"][0][1] - 0.05) < 0.02


def test_cancel_throws_changes_away(server, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto(server["url"])
    sync_api.expect(page.locator("#lineList [data-name='entrance']")).to_be_visible(timeout=10000)
    before = server["config"].read_text(encoding="utf-8")
    page.click("#btnEdit")
    page.click("#shapeList button >> text=entrance")
    page.click("#btnDelete")
    page.on("dialog", lambda dialog: dialog.accept())
    page.click("#btnCancel")
    sync_api.expect(page.locator("#lineList [data-name='entrance']")).to_be_visible()
    assert server["config"].read_text(encoding="utf-8") == before


def test_csv_download(server, browser):
    page = browser.new_page(viewport={"width": 1400, "height": 900}, accept_downloads=True)
    page.goto(server["url"])
    page.click("#btnExport")
    with page.expect_download() as info:
        page.click("#linkCsv")
    download = info.value
    assert download.suggested_filename.endswith("_hourly.csv")
    text = Path(download.path()).read_text(encoding="utf-8-sig")
    assert text.startswith("date,hour,camera,type,name,in,out")


def test_phone_layout_has_no_sideways_scroll(server, browser):
    page = browser.new_page(viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True)
    page.goto(server["url"])
    sync_api.expect(page.locator("#lineList [data-name='entrance']")).to_be_visible(timeout=10000)
    width = page.evaluate("document.documentElement.scrollWidth")
    assert width <= 390
    # numbers come before the picture on a phone
    numbers = page.locator("#numbers").bounding_box()
    stage = page.locator("#stage").bounding_box()
    assert numbers["y"] < stage["y"]
