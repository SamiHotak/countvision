"""Fixtures shared by all tests."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from countvision_edge.app import LiveRunner, create_app
from countvision_edge.config import load_config
from countvision_edge.detectors import build_detector
from countvision_edge.storage import SqliteBuffer
from countvision_edge.testing.synthetic import demo_scene


@pytest.fixture(scope="session")
def demo_video(tmp_path_factory) -> str:
    """The synthetic demo scene as an .avi file (created once per test run)."""
    folder = tmp_path_factory.mktemp("video")
    return str(demo_scene().write_video(folder / "demo.avi"))


# ------------------------------------------------------------------ local web app

CONFIG_TEMPLATE = """\
# Test config. This comment must survive saving lines from the browser.
data_dir: {data_dir}
heartbeat_interval_s: 3600
detector:
  type: blobs   # demo detector, no model
cameras:
  - id: demo
    name: Demo scene
    source:
      uri: {video}
      realtime: false
    classes: [person, car]
    lines:
      - name: entrance
        p1: [0.1, 0.5]
        p2: [0.9, 0.5]
        in_direction: to_right
        deadband_px: 3   # kept when the line is moved in the browser
    zones:
      - name: waiting
        kind: queue
        polygon: [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]]
"""

WRITE = {"X-CountVision": "1"}


@pytest.fixture
def app_config(tmp_path: Path, demo_video: str) -> Path:
    video = tmp_path / "demo.avi"
    shutil.copy(demo_video, video)
    path = tmp_path / "config.yaml"
    path.write_text(
        CONFIG_TEMPLATE.format(data_dir=(tmp_path / "data").as_posix(), video=video.as_posix()),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def runner(app_config: Path):
    cfg = load_config(app_config)
    buffer = SqliteBuffer(cfg.db_file())
    run = LiveRunner(cfg, cfg.camera(), build_detector(cfg.detector), buffer, config_path=app_config)
    yield run
    run.stop()
    buffer.close()


@pytest.fixture
def client(runner):
    from fastapi.testclient import TestClient

    app = create_app(runner, trusted_hosts={"testserver", "127.0.0.1", "localhost"})
    with TestClient(app) as test_client:
        assert runner.wait(60), "demo video did not finish"
        yield test_client
