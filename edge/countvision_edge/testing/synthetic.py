"""Synthetic scenes: moving coloured rectangles with known ground truth.

Used by the tests and by ``countvision-edge demo``. Red rectangle = person, blue = car.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

PERSON_BGR = (0, 0, 255)
CAR_BGR = (255, 0, 0)
BACKGROUND_BGR = (45, 45, 45)
CLASS_IDS = {"person": 0, "car": 2}


@dataclass
class SyntheticObject:
    """A rectangle that follows ``waypoints`` = [(t_seconds, centre_x, centre_y), ...].

    Between waypoints it moves in a straight line at constant speed. It exists only between
    the first and the last waypoint time.
    """

    kind: str  # "person" or "car"
    waypoints: list[tuple[float, float, float]]
    size: tuple[int, int] = (30, 70)

    def centre_at(self, t: float) -> tuple[float, float] | None:
        points = self.waypoints
        if t < points[0][0] or t > points[-1][0]:
            return None
        for (t0, x0, y0), (t1, x1, y1) in zip(points, points[1:], strict=False):
            if t0 <= t <= t1:
                f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
                return (x0 + f * (x1 - x0), y0 + f * (y1 - y0))
        return None

    def box_at(self, t: float) -> tuple[float, float, float, float] | None:
        centre = self.centre_at(t)
        if centre is None:
            return None
        w, h = self.size
        return (centre[0] - w / 2, centre[1] - h / 2, centre[0] + w / 2, centre[1] + h / 2)


@dataclass
class SyntheticScene:
    width: int = 640
    height: int = 360
    fps: float = 15.0
    duration_s: float = 10.0
    objects: list[SyntheticObject] = field(default_factory=list)

    @property
    def n_frames(self) -> int:
        return int(round(self.duration_s * self.fps))

    def ground_truth(self, index: int) -> list[tuple[float, float, float, float, int]]:
        """Exact boxes (x1, y1, x2, y2, class_id) of frame ``index``."""
        t = index / self.fps
        boxes = []
        for obj in self.objects:
            box = obj.box_at(t)
            if box is not None:
                boxes.append((*box, CLASS_IDS[obj.kind]))
        return boxes

    def render(self, index: int) -> np.ndarray:
        image = np.full((self.height, self.width, 3), BACKGROUND_BGR, dtype=np.uint8)
        for x1, y1, x2, y2, class_id in self.ground_truth(index):
            color = PERSON_BGR if class_id == 0 else CAR_BGR
            cv2.rectangle(image, (round(x1), round(y1)), (round(x2) - 1, round(y2) - 1), color, -1)
        return image

    def write_video(self, path: str | Path) -> Path:
        """Write the scene to a video file. ``.avi`` uses MJPG, anything else mp4v."""
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        code = "MJPG" if out.suffix.lower() == ".avi" else "mp4v"
        fourcc = (
            cv2.VideoWriter_fourcc(*code)
            if hasattr(cv2, "VideoWriter_fourcc")
            else cv2.VideoWriter.fourcc(*code)
        )
        writer = cv2.VideoWriter(str(out), fourcc, self.fps, (self.width, self.height))
        if not writer.isOpened():
            raise RuntimeError(f"Could not create video file {out} (codec {code})")
        try:
            for i in range(self.n_frames):
                writer.write(self.render(i))
        finally:
            writer.release()
        return out


def demo_scene() -> SyntheticScene:
    """The standard demo scene (640x360, 15 FPS, 18 s).

    A horizontal counting line at the middle of the image. Moving DOWN across it is IN.
      * 3 people walk down across the line -> person IN = 3
      * 2 people walk up across the line -> person OUT = 2
      * 1 car drives down across the line -> car IN = 1
      * 1 person walks down at the far left edge, outside the line's extent -> not counted
    A "waiting" zone in the lower left: one person walks in, stands still for 6 s, leaves.
    """

    def walk(kind, x, t0, t1, y0, y1, size=(30, 70)):
        return SyntheticObject(kind, [(t0, x, y0), (t1, x, y1)], size)

    return SyntheticScene(
        duration_s=18.0,
        objects=[
            walk("person", 330, 1, 5, 60, 300),  # A  down -> IN
            walk("person", 400, 3, 7, 60, 300),  # B  down -> IN
            walk("person", 500, 5, 9, 60, 300),  # C  down -> IN
            walk("person", 20, 2, 6, 60, 300),  # D  down, outside the line -> not counted
            walk("person", 360, 8, 12, 300, 60),  # E  up -> OUT
            walk("person", 450, 10, 14, 300, 60),  # F  up -> OUT
            walk("car", 560, 12, 16, 60, 300, size=(90, 50)),  # G  car down -> IN
            SyntheticObject(  # H  walks into the waiting zone, waits, walks out
                "person",
                [(2, 330, 290), (5, 160, 290), (11, 160, 290), (14, 330, 290)],
            ),
        ],
    )


# Expected result of the demo scene, written by hand from the description above.
DEMO_EXPECTED = {
    "line": "door",
    "person_in": 3,
    "person_out": 2,
    "car_in": 1,
    "car_out": 0,
}
