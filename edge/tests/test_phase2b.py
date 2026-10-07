"""Phase 2 B: privacy preview, model download, example configs, pilot papers."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import numpy as np
import pytest

from countvision_edge import cli
from countvision_edge.app.runner import blur_boxes
from countvision_edge.config import load_config, parse_config
from countvision_edge.errors import ConfigError, DetectorError
from countvision_edge.models import MODELS, ModelInfo, download, ensure_model, find_by_file
from countvision_edge.pilot.docs import BLANK, DOCUMENTS, PilotInfo, load_info, render, write_pack
from countvision_edge.pipeline import FrameResult
from countvision_edge.privacy import limit_width, pixelate_boxes
from countvision_edge.types import Frame

from .helpers import make_track

ROOT = Path(__file__).resolve().parents[2]
CONFIGS = ROOT / "edge" / "configs"


# ------------------------------------------------------------------ privacy


def noisy(h=200, w=300) -> np.ndarray:
    return np.random.default_rng(1).integers(0, 255, (h, w, 3), dtype=np.uint8)


def test_pixelate_changes_only_the_boxes():
    image = noisy()
    original = image.copy()
    pixelate_boxes(image, [(100, 50, 160, 150)], margin=0.0)
    assert not np.array_equal(image[50:150, 100:160], original[50:150, 100:160])
    assert np.array_equal(image[:, :90], original[:, :90])  # outside untouched
    # pixelated = blocks of equal colour
    assert len(np.unique(image[60:72, 100:112].reshape(-1, 3), axis=0)) <= 4


def test_blur_covers_fresh_detections_not_only_tracks():
    """A person in the first frames (no confirmed track yet) must be pixelated too."""
    image = noisy()
    original = image.copy()
    frame = Frame(image, 0, 0.0, 0.0)
    result = FrameResult(frame, 1, tracks=[make_track(1, 60, 120, w=40, h=80)], events=[], proc_ms=1.0,
                         boxes=np.array([[200, 40, 260, 140]], np.float32))
    blur_boxes(image, result)
    assert not np.array_equal(image[50:130, 210:250], original[50:130, 210:250])  # fresh detection
    assert not np.array_equal(image[50:110, 45:75], original[50:110, 45:75])  # confirmed track


def test_limit_width_never_upscales():
    assert limit_width(noisy(100, 2000), 640).shape == (32, 640, 3)
    small = noisy(100, 300)
    assert limit_width(small, 640) is small


def test_privacy_defaults_and_env_override():
    base = {"detector": {"type": "blobs"}, "cameras": [{"source": {"uri": "x.mp4"}}]}
    cfg = parse_config(base, env={})
    assert cfg.privacy.preview == "blur" and cfg.alerts.camera_offline_after_s == 300
    assert parse_config(base, env={"CV_PREVIEW": "off"}).privacy.preview == "off"
    with pytest.raises(ConfigError):
        parse_config(base | {"privacy": {"preview": "sharp"}}, env={})


# ------------------------------------------------------------------ models


class FakeResponse(io.BytesIO):
    headers = {"Content-Length": "11"}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def fake_model(data: bytes = b"hello model") -> ModelInfo:
    return ModelInfo("fake", "fake.onnx", "https://example.invalid/fake.onnx",
                     hashlib.sha256(data).hexdigest(), 0.1, "MIT", "test")


def test_download_checks_the_checksum(tmp_path):
    info = fake_model()
    path = download(info, tmp_path / "m" / "fake.onnx", opener=lambda url: FakeResponse(b"hello model"))
    assert path.read_bytes() == b"hello model"
    # already there and correct: no second download
    assert download(info, path, opener=lambda url: pytest.fail("downloaded again")) == path
    with pytest.raises(DetectorError, match="damaged"):
        download(info, tmp_path / "bad.onnx", opener=lambda url: FakeResponse(b"evil bytes!"))
    assert not (tmp_path / "bad.onnx").exists() and not (tmp_path / "bad.onnx.part").exists()


def test_known_models_and_ensure_model(tmp_path):
    assert set(MODELS) == {"yolox_tiny", "yolox_nano", "yolox_s"}
    assert all(m.license.startswith("Apache-2.0") for m in MODELS.values())
    assert find_by_file("models/yolox_tiny.onnx").name == "yolox_tiny"
    assert find_by_file("my_model.onnx") is None
    existing = tmp_path / "custom.onnx"
    existing.write_bytes(b"x")
    assert ensure_model(existing) == existing
    with pytest.raises(DetectorError, match="not found"):
        ensure_model(tmp_path / "unknown.onnx")


def test_models_command_lists(capsys):
    assert cli.main(["models", "list"]) == 0
    assert "yolox_tiny" in capsys.readouterr().out
    assert cli.main(["models", "download", "nope"]) == 2


# ------------------------------------------------------------------ example configs


def test_example_configs_use_the_permissive_default():
    cfg = load_config(CONFIGS / "example.yaml", env={})
    assert cfg.detector.type == "onnx" and cfg.detector.model == "models/yolox_tiny.onnx"
    assert cfg.detector.model_license.startswith("Apache-2.0")
    site = load_config(CONFIGS / "site.example.yaml", env={"CAM1_URL": "rtsp://u:p@h/1", "CAM2_URL": "rtsp://h/2"})
    assert [c.id for c in site.cameras] == ["entrance", "counter"] and site.storage.retention_days == 45
    with pytest.raises(ConfigError, match="CAM1_URL"):
        load_config(CONFIGS / "site.example.yaml", env={})
    docker = load_config(ROOT / "docker" / "test-camera" / "config.yaml",
                         env={"CAM1_URL": "rtsp://test-camera:8554/cam1", "CAM2_URL": "rtsp://x/2",
                              "CV_MODELS": "/opt/models"})
    assert docker.detector.model == "/opt/models/yolox_tiny.onnx" and len(docker.cameras) == 2


# ------------------------------------------------------------------ pilot papers


def test_pilot_pack_example_fills_every_document(tmp_path):
    info = load_info(ROOT / "docs" / "pilot" / "pilot.example.yaml")
    files = write_pack(info, tmp_path)
    assert [f.name for f in files] == ["index.html"] + [d.file for d in DOCUMENTS]
    sign = (tmp_path / "hinweisschild.html").read_text(encoding="utf-8")
    assert "Café Beispiel GmbH" in sign and "keine Bilder gespeichert" in sign
    assert "{{" not in sign and "<style>" in sign
    agreement = (tmp_path / "pilotvereinbarung.html").read_text(encoding="utf-8")
    assert "über der Eingangstür" in agreement and "01.11.2026" in agreement
    assert BLANK in agreement  # the provider fields are empty in the example -> lines to fill in


def test_blank_pack_and_escaping(tmp_path):
    write_pack(PilotInfo(), tmp_path)
    for doc in DOCUMENTS:
        text = (tmp_path / doc.file).read_text(encoding="utf-8")
        assert "{{" not in text
    evil = PilotInfo.model_validate({"business": {"name": "<script>alert(1)</script>"}})
    write_pack(evil, tmp_path / "evil")
    text = (tmp_path / "evil" / "hinweisschild.html").read_text(encoding="utf-8")
    assert "<script>alert" not in text and "&lt;script&gt;" in text


def test_render_refuses_unknown_fields():
    assert render("Hi {{ name }}", {"name": "Ezat"}) == "Hi Ezat"
    with pytest.raises(ConfigError, match="unknown field"):
        render("{{ nmae }}", {"name": "x"})


def test_pilot_docs_command(tmp_path, capsys):
    assert cli.main(["pilot-docs", "--blank", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "index.html").is_file()
    assert "not legal advice" in capsys.readouterr().out
    assert cli.main(["pilot-docs", "--out", str(tmp_path)]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("business: {nmae: x}\n", encoding="utf-8")
    assert cli.main(["pilot-docs", "--info", str(bad), "--out", str(tmp_path)]) == 2
