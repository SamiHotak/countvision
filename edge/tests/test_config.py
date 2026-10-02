from __future__ import annotations

from pathlib import Path

import pytest

from countvision_edge.config import (
    SourceConfig,
    expand_env,
    load_config,
    parse_config,
    redact_uri,
)
from countvision_edge.errors import ConfigError

CONFIGS = Path(__file__).parents[1] / "configs"


def minimal(**camera) -> dict:
    cam = {"id": "c1", "source": {"uri": "video.mp4"}, **camera}
    return {"detector": {"type": "blobs"}, "cameras": [cam]}


def test_example_configs_are_valid():
    cfg = load_config(CONFIGS / "example.yaml")
    assert cfg.cameras[0].source.kind == "webcam"
    assert cfg.cameras[0].lines[0].in_direction == "to_right"
    rtsp = load_config(CONFIGS / "example_rtsp_onnx.yaml", env={"CAM1_RTSP_URL": "rtsp://u:p@h/s"})
    assert rtsp.cameras[0].source.kind == "rtsp"
    assert rtsp.cameras[0].speed is not None and rtsp.detector.type == "openvino"


@pytest.mark.parametrize(
    ("uri", "kind"),
    [
        ("0", "webcam"),
        (1, "webcam"),
        ("rtsp://host/stream", "rtsp"),
        ("RTSPS://host/stream", "rtsp"),
        ("http://host/video.mjpg", "http"),
        ("clips/video.mp4", "file"),
    ],
)
def test_source_kind_is_detected(uri, kind):
    assert SourceConfig(uri=uri).kind == kind


def test_live_default_fps_is_10_and_files_process_everything():
    live = parse_config(minimal(source={"uri": "0"}), env={}).cameras[0]
    file = parse_config(minimal(), env={}).cameras[0]
    assert live.effective_target_fps() == 10.0 and file.effective_target_fps() is None
    explicit = parse_config(minimal(scheduler={"target_fps": 5}), env={}).cameras[0]
    assert explicit.effective_target_fps() == 5.0


def test_environment_variables_are_expanded_with_defaults():
    data = {"a": "${X}", "b": ["${Y:-fallback}", "plain"], "c": {"d": "pre-${X}-post"}}
    assert expand_env(data, {"X": "1"}) == {"a": "1", "b": ["fallback", "plain"], "c": {"d": "pre-1-post"}}


def test_missing_environment_variable_is_a_clear_error_without_secrets():
    with pytest.raises(ConfigError, match="CAM_URL"):
        parse_config(minimal(source={"uri": "${CAM_URL}"}), env={})


def test_typos_are_errors():
    with pytest.raises(ConfigError, match="in_directon"):
        parse_config(minimal(lines=[{"name": "l", "p1": [0, 0.5], "p2": [1, 0.5], "in_directon": "to_right"}]), env={})


def test_normalized_points_must_be_between_0_and_1():
    with pytest.raises(ConfigError, match="outside 0..1"):
        parse_config(minimal(lines=[{"name": "l", "p1": [10, 200], "p2": [900, 200]}]), env={})
    ok = parse_config(minimal(coordinates="pixel", lines=[{"name": "l", "p1": [10, 200], "p2": [900, 200]}]), env={})
    assert ok.cameras[0].lines[0].p2 == (900, 200)


def test_line_needs_two_different_points_and_zone_needs_three():
    with pytest.raises(ConfigError, match="different"):
        parse_config(minimal(lines=[{"name": "l", "p1": [0.5, 0.5], "p2": [0.5, 0.5]}]), env={})
    with pytest.raises(ConfigError):
        parse_config(minimal(zones=[{"name": "z", "polygon": [[0, 0], [1, 1]]}]), env={})


def test_names_must_be_unique():
    line = {"name": "l", "p1": [0, 0.5], "p2": [1, 0.5]}
    with pytest.raises(ConfigError, match="unique"):
        parse_config(minimal(lines=[line, line]), env={})
    cfg = minimal()
    cfg["cameras"].append(dict(cfg["cameras"][0]))
    with pytest.raises(ConfigError, match="unique"):
        parse_config(cfg, env={})


def test_environment_overrides():
    cfg = parse_config(minimal(), env={"CV_DB_PATH": "/x/y.db", "CV_DATA_DIR": "/d", "CV_LOG_LEVEL": "DEBUG",
                                       "CV_DEVICE_ID": "dev-1"})
    assert str(cfg.db_file()) == "/x/y.db" and cfg.data_dir == "/d"
    assert cfg.log_level == "DEBUG" and cfg.resolve_device_id() == "dev-1"


def test_device_id_is_created_once_and_kept(tmp_path):
    cfg = parse_config({**minimal(), "data_dir": str(tmp_path / "data")}, env={})
    first = cfg.resolve_device_id()
    assert first.startswith("edge-")
    assert parse_config({**minimal(), "data_dir": str(tmp_path / "data")}, env={}).resolve_device_id() == first


def test_default_db_path_is_inside_data_dir():
    cfg = parse_config({**minimal(), "data_dir": "somewhere"}, env={})
    assert cfg.db_file() == Path("somewhere") / "countvision.db"


def test_camera_selection():
    data = minimal()
    data["cameras"].append({"id": "c2", "source": {"uri": "other.mp4"}})
    cfg = parse_config(data, env={})
    with pytest.raises(ConfigError, match="several cameras"):
        cfg.camera()
    assert cfg.camera("c2").id == "c2"
    with pytest.raises(ConfigError, match="No camera"):
        cfg.camera("nope")


def test_load_config_errors(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "missing.yaml")
    bad = tmp_path / "bad.yaml"
    bad.write_text("cameras: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="YAML"):
        load_config(bad)
    bad.write_text("- just\n- a list\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="mapping"):
        load_config(bad)


def test_redact_uri_hides_credentials():
    assert redact_uri("rtsp://admin:hunter2@192.168.1.5:554/s") == "rtsp://***@192.168.1.5:554/s"
    assert redact_uri("rtsp://192.168.1.5/s") == "rtsp://192.168.1.5/s"


def test_start_time_is_utc_when_naive():
    cfg = SourceConfig(uri="v.mp4", start_time="2026-01-01T09:00:00")
    assert cfg.start_epoch() == 1767258000.0
