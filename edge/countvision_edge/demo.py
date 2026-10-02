"""Self-test: run the whole pipeline on a synthetic video with known answers.

``countvision-edge demo`` needs no camera and no model. If it prints PASS, the install works
and the counting logic gives the exact expected numbers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import EdgeConfig, parse_config
from .detectors import build_detector
from .inputs import create_source
from .pipeline import CameraPipeline, RunSummary
from .storage import SqliteBuffer
from .testing.synthetic import DEMO_EXPECTED, demo_scene
from .viz import Annotator, VideoRecorder


def demo_config(video: str | Path, data_dir: str | Path, start_time: str) -> EdgeConfig:
    """Config for the demo scene: one counting line and one queue zone."""
    return parse_config(
        {
            "data_dir": str(data_dir),
            "detector": {"type": "blobs"},
            "heartbeat_interval_s": 3600,
            "cameras": [
                {
                    "id": "demo",
                    "source": {"uri": str(video), "start_time": start_time},
                    "classes": ["person", "car"],
                    "lines": [
                        {
                            "name": DEMO_EXPECTED["line"],
                            "p1": [0.1, 0.5],
                            "p2": [0.9, 0.5],
                            "in_direction": "to_right",
                        }
                    ],
                    "zones": [
                        {
                            "name": "waiting",
                            "polygon": [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]],
                            "kind": "queue",
                        }
                    ],
                }
            ],
        },
        env={},
    )


@dataclass
class Check:
    name: str
    expected: str
    actual: str
    ok: bool


@dataclass
class DemoResult:
    checks: list[Check] = field(default_factory=list)
    summary: RunSummary | None = None
    db_path: Path | None = None
    video_path: Path | None = None

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def run_demo(
    out_dir: str | Path, start_time: str | None = None, annotated: bool = False
) -> DemoResult:
    """Generate the demo video, count it, and compare with the hand-written expectation.

    With ``annotated=True`` an annotated video is written next to it (demo_annotated.mp4).
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    start = start_time or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")  # noqa: UP017
    video = demo_scene().write_video(out / "demo_scene.avi")
    cfg = demo_config(video, out / "data", start)
    cam = cfg.camera()

    db_path = out / "demo.db"
    db_path.unlink(missing_ok=True)
    buffer = SqliteBuffer(db_path)
    pipeline = CameraPipeline(cam, build_detector(cfg.detector), buffer, data_dir=cfg.data_dir)
    recorder = annotator = None
    if annotated:
        recorder = VideoRecorder(str(out / "demo_annotated.mp4"), demo_scene().fps)
        annotator = Annotator(lambda: pipeline.engine)
    try:
        summary = pipeline.run(
            create_source(cam.source),
            on_result=(lambda r: recorder.write(annotator.draw(r))) if annotator else None,
        )
    finally:
        if recorder is not None:
            recorder.close()

    result = DemoResult(summary=summary, db_path=db_path, video_path=video)
    lines = {
        r["class_name"]: (r["in_count"], r["out_count"])
        for r in _sum_lines(buffer.query("line_counts", camera_id="demo"))
    }
    for label, cls, idx, expected in (
        ("person IN", "person", 0, DEMO_EXPECTED["person_in"]),
        ("person OUT", "person", 1, DEMO_EXPECTED["person_out"]),
        ("car IN", "car", 0, DEMO_EXPECTED["car_in"]),
        ("car OUT", "car", 1, DEMO_EXPECTED["car_out"]),
    ):
        actual = lines.get(cls, (0, 0))[idx]
        result.checks.append(Check(label, str(expected), str(actual), actual == expected))

    visits = [e for e in buffer.query("events", camera_id="demo") if e["kind"] == "zone_visit"]
    dwell = visits[0]["dwell_s"] if len(visits) == 1 else None
    result.checks.append(
        Check(
            "waiting zone: 1 visit, dwell 9.5-11.5 s",
            "1 visit, ~10.4 s",
            "no single visit" if dwell is None else f"{len(visits)} visit, {dwell:.1f} s",
            dwell is not None and 9.5 <= dwell <= 11.5,
        )
    )
    buffer.close()
    return result


def _sum_lines(rows: list[dict]) -> list[dict]:
    """Sum per-minute rows into one row per class (the '*' total row is skipped)."""
    totals: dict[str, dict] = {}
    for row in rows:
        if row["class_name"] == "*":
            continue
        name = row["class_name"]
        t = totals.setdefault(name, {"class_name": name, "in_count": 0, "out_count": 0})
        t["in_count"] += row["in_count"]
        t["out_count"] += row["out_count"]
    return list(totals.values())
