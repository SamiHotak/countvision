"""count-helper: label the TRUE counts of a video by hand, quickly.

    countvision-edge count-helper eval/videos/shop.mp4 --labels eval/clips/shop.yaml

You watch the video and press a key at the moment a person (or car) crosses the line:
``I`` = in, ``O`` = out. In a zone you type the number of people you see (0-9). Everything is
saved to a YAML label file after every change, so nothing is lost.

The model's detections are deliberately NOT shown: labels must be what a human sees,
otherwise we would only measure "the model agrees with itself".

The logic (``LabelSession``) has no GUI code and is unit-tested. ``run_gui`` is the thin
OpenCV window around it (needs opencv-python with GUI support, which is the default
install on Windows).
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from ..config import LineConfig, ZoneConfig
from ..errors import CountVisionError
from .cache import probe_video
from .labels import ClipLabels, GtCrossing, GtOccupancy, load_labels, save_labels

log = logging.getLogger(__name__)

HELP = [
    "SPACE play/pause    A/D or arrows: -/+ 1 frame    J/L: -/+ 1 s    [ ]: speed",
    "I = IN   O = OUT (active line, at this moment)    X = delete nearest mark    U = undo",
    "0-9 = people in the active zone now   + / - = change that number   G = next occupancy stop",
    "N = next line/zone   F = flip IN direction   C = next class   , . = previous/next mark",
    "T = tool: draw line (2 clicks) / draw zone (clicks, right-click to close) / off",
    "Saved after every change.   H = hide help   ESC = quit",
]

ARROWS = {2424832: "left", 2555904: "right", 2490368: "up", 2621440: "down",  # Windows
          65361: "left", 65363: "right", 65362: "up", 65364: "down",        # Linux GTK
          63234: "left", 63235: "right", 63232: "up", 63233: "down"}        # macOS


@dataclass
class LabelSession:
    """Labeling state and actions. Times are seconds from the start of the video."""

    labels: ClipLabels
    fps: float
    n_frames: int
    path: Path | None = None
    frame: int = 0
    active: int = 0  # index into targets()
    cls_index: int = 0
    occupancy_every_s: float = 10.0
    tool: str = "off"  # off | line | zone
    pending: list[tuple[float, float]] = field(default_factory=list)
    message: str = ""
    _undo: list[str] = field(default_factory=list)

    # -- basics -------------------------------------------------------------------------

    @property
    def t(self) -> float:
        return round(self.frame / self.fps, 3)

    @property
    def cls(self) -> str:
        return self.labels.classes[self.cls_index % len(self.labels.classes)]

    def targets(self) -> list[tuple[str, str]]:
        """("line", name) and ("zone", name) in order. N cycles through them."""
        return [("line", line.name) for line in self.labels.lines] + [
            ("zone", zone.name) for zone in self.labels.zones
        ]

    def active_target(self) -> tuple[str, str] | None:
        items = self.targets()
        return items[self.active % len(items)] if items else None

    def seek(self, frame: int) -> None:
        self.frame = max(0, min(self.n_frames - 1, int(frame)))

    def step_seconds(self, seconds: float) -> None:
        self.seek(self.frame + round(seconds * self.fps))

    # -- changes (all saved, all undoable) ----------------------------------------------

    def _change(self) -> None:
        self._undo.append(self.labels.model_dump_json())
        if len(self._undo) > 200:
            self._undo.pop(0)

    def _replace(self, **update) -> None:
        self.labels = ClipLabels.model_validate(self.labels.model_dump() | update)
        self.save()

    def mark(self, direction: str) -> bool:
        target = self.active_target()
        if target is None or target[0] != "line":
            self.message = "Select a line first (N), or draw one (T)"
            return False
        self._change()
        new = GtCrossing(t=self.t, line=target[1], dir=direction, cls=self.cls)
        crossings = [*self.labels.crossings, new]
        self._replace(crossings=[c.model_dump() for c in crossings])
        self.message = f"{direction.upper()} {self.cls} on '{target[1]}' at {self.t:.2f}s"
        return True

    def delete_nearest(self, window_s: float = 1.0) -> bool:
        target = self.active_target()
        items = self.labels.crossings
        if target and target[0] == "line":
            candidates = [c for c in items if c.line == target[1]]
        else:
            candidates = list(items)
        if not candidates:
            return False
        nearest = min(candidates, key=lambda c: abs(c.t - self.t))
        if abs(nearest.t - self.t) > window_s:
            self.message = f"No mark within {window_s:.0f}s"
            return False
        self._change()
        rest = [c.model_dump() for c in items if c is not nearest]
        self._replace(crossings=rest)
        self.message = f"Deleted {nearest.dir.upper()} at {nearest.t:.2f}s"
        return True

    def set_occupancy(self, n: int) -> bool:
        target = self.active_target()
        if target is None or target[0] != "zone":
            self.message = "Select a zone first (N), or draw one (T)"
            return False
        self._change()
        kept = [o.model_dump() for o in self.labels.occupancy
                if not (o.zone == target[1] and abs(o.t - self.t) < 1e-3)]
        kept.append(GtOccupancy(t=self.t, zone=target[1], n=max(0, n)).model_dump())
        self._replace(occupancy=kept)
        self.message = f"Zone '{target[1]}' at {self.t:.2f}s: {max(0, n)}"
        return True

    def adjust_occupancy(self, delta: int) -> bool:
        target = self.active_target()
        if target is None or target[0] != "zone":
            return False
        current = next((o.n for o in self.labels.occupancy
                        if o.zone == target[1] and abs(o.t - self.t) < 1e-3), 0)
        return self.set_occupancy(current + delta)

    def next_occupancy_stop(self) -> None:
        """Jump to the next multiple of ``occupancy_every_s``."""
        step = self.occupancy_every_s
        nxt = (int(self.t / step + 1e-6) + 1) * step
        self.seek(round(nxt * self.fps))

    def jump_mark(self, forward: bool) -> bool:
        times = sorted({c.t for c in self.labels.crossings} | {o.t for o in self.labels.occupancy})
        if forward:
            later = [t for t in times if t > self.t + 0.5]
            if not later:
                return False
            self.seek(round(later[0] * self.fps) - round(self.fps))  # 1 s before the mark
        else:
            earlier = [t for t in times if t < self.t - 0.05]
            if not earlier:
                return False
            self.seek(round(earlier[-1] * self.fps) - round(self.fps))
        return True

    def flip(self) -> bool:
        target = self.active_target()
        if target is None or target[0] != "line":
            return False
        self._change()
        lines = []
        for line in self.labels.lines:
            data = line.model_dump()
            if line.name == target[1]:
                data["in_direction"] = "to_left" if line.in_direction == "to_right" else "to_right"
            lines.append(data)
        self._replace(lines=lines)
        self.message = f"Line '{target[1]}': IN direction flipped"
        return True

    def add_line(self, p1: tuple[float, float], p2: tuple[float, float], name: str | None = None) -> bool:
        if p1 == p2:
            return False
        self._change()
        name = name or self._free_name("line")
        line = LineConfig(name=name, p1=_r(p1), p2=_r(p2))
        self._replace(lines=[*[x.model_dump() for x in self.labels.lines], line.model_dump()])
        self.active = len(self.labels.lines) - 1
        self.message = f"Line '{name}' added. Arrow = IN direction (F flips)"
        return True

    def add_zone(self, polygon: list[tuple[float, float]], name: str | None = None) -> bool:
        if len(polygon) < 3:
            self.message = "A zone needs at least 3 points"
            return False
        self._change()
        name = name or self._free_name("zone")
        zone = ZoneConfig(name=name, polygon=[_r(p) for p in polygon])
        self._replace(zones=[*[x.model_dump() for x in self.labels.zones], zone.model_dump()])
        self.active = len(self.labels.lines) + len(self.labels.zones) - 1
        self.message = f"Zone '{name}' added. Type 0-9 = people inside now"
        return True

    def click(self, point: tuple[float, float]) -> None:
        """A left click in normalised coordinates while a drawing tool is on."""
        if self.tool == "line":
            self.pending.append(point)
            if len(self.pending) == 2:
                self.add_line(self.pending[0], self.pending[1])
                self.pending.clear()
                self.tool = "off"
        elif self.tool == "zone":
            self.pending.append(point)

    def close_zone(self) -> None:
        if self.tool == "zone":
            if self.add_zone(list(self.pending)):
                self.tool = "off"
            self.pending.clear()

    def undo(self) -> bool:
        if not self._undo:
            self.message = "Nothing to undo"
            return False
        self.labels = ClipLabels.model_validate_json(self._undo.pop())
        self.save()
        self.message = "Undone"
        return True

    def save(self) -> None:
        if self.path is not None:
            save_labels(self.labels, self.path)

    def _free_name(self, prefix: str) -> str:
        used = {x.name for x in [*self.labels.lines, *self.labels.zones]}
        i = 1
        while f"{prefix}{i}" in used:
            i += 1
        return f"{prefix}{i}"

    # -- keys ---------------------------------------------------------------------------

    def handle_key(self, key: str) -> None:
        """Apply one key (lower-case letter, digit, or arrow name). Playback keys are
        handled by the GUI loop."""
        actions = {
            "i": lambda: self.mark("in"), "o": lambda: self.mark("out"),
            "x": self.delete_nearest, "u": self.undo, "f": self.flip,
            "g": self.next_occupancy_stop,
            "+": lambda: self.adjust_occupancy(1), "=": lambda: self.adjust_occupancy(1),
            "-": lambda: self.adjust_occupancy(-1),
            ".": lambda: self.jump_mark(True), ",": lambda: self.jump_mark(False),
            "a": lambda: self.seek(self.frame - 1), "left": lambda: self.seek(self.frame - 1),
            "d": lambda: self.seek(self.frame + 1), "right": lambda: self.seek(self.frame + 1),
            "j": lambda: self.step_seconds(-1), "down": lambda: self.step_seconds(-1),
            "l": lambda: self.step_seconds(1), "up": lambda: self.step_seconds(1),
        }
        if key.isdigit() and len(key) == 1:
            self.set_occupancy(int(key))
        elif key == "n":
            if self.targets():
                self.active = (self.active + 1) % len(self.targets())
                kind, name = self.active_target() or ("", "")
                self.message = f"Active {kind}: '{name}'"
        elif key == "c":
            self.cls_index = (self.cls_index + 1) % len(self.labels.classes)
            self.message = f"Class: {self.cls}"
        elif key == "t":
            self.tool = {"off": "line", "line": "zone", "zone": "off"}[self.tool]
            self.pending.clear()
            self.message = {"line": "Click 2 points for a line", "zone":
                            "Click the corners, right-click to close", "off": "Tool off"}[self.tool]
        elif key in actions:
            actions[key]()

    def summary(self) -> str:
        parts = []
        for line in self.labels.lines:
            ins = sum(1 for c in self.labels.crossings if c.line == line.name and c.dir == "in")
            outs = sum(1 for c in self.labels.crossings if c.line == line.name and c.dir == "out")
            parts.append(f"{line.name}: IN {ins} OUT {outs}")
        for zone in self.labels.zones:
            n = sum(1 for o in self.labels.occupancy if o.zone == zone.name)
            parts.append(f"{zone.name}: {n} samples")
        return " | ".join(parts) or "no lines or zones yet (press T)"


def _r(point: tuple[float, float]) -> tuple[float, float]:
    return (round(min(max(point[0], 0.0), 1.0), 4), round(min(max(point[1], 0.0), 1.0), 4))


# ------------------------------------------------------------------------------- session setup


def open_session(
    video: str | Path,
    labels_path: str | Path,
    *,
    clip: str | None = None,
    classes: list[str] | None = None,
    split: str = "dev",
    scene: str = "clear",
    anchor: str = "bottom_center",
    labeled_by: str | None = None,
    occupancy_every_s: float = 10.0,
) -> LabelSession:
    """Load the label file if it exists, otherwise start a new one for this video."""
    info = probe_video(video)
    path = Path(labels_path)
    if path.exists():
        labels = load_labels(path)
    else:
        labels = ClipLabels(
            clip=clip or _slug(Path(video).stem),
            video=Path(video).name,
            split=split,  # type: ignore[arg-type]
            scene=scene,  # type: ignore[arg-type]
            classes=classes or ["person"],
            anchor=anchor,  # type: ignore[arg-type]
            labeled_by=labeled_by,
        )
        save_labels(labels, path)
    return LabelSession(labels, info.fps, info.n_frames, path, occupancy_every_s=occupancy_every_s)


def _slug(text: str) -> str:
    out = "".join(ch if ch.isalnum() or ch in "_-" else "-" for ch in text)[:64].strip("-")
    return out or "clip"


# ------------------------------------------------------------------------------- GUI


class _FrameReader:
    """Sequential reading with a small cache, so stepping back a few frames is instant."""

    def __init__(self, video: str | Path, keep: int = 150) -> None:
        self.cap = cv2.VideoCapture(str(video))
        if not self.cap.isOpened():
            raise CountVisionError(f"Could not open video: {video}")
        self.position = 0  # index of the next frame cap.read() returns
        self.cache: OrderedDict[int, np.ndarray] = OrderedDict()
        self.keep = keep

    def get(self, index: int) -> np.ndarray | None:
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        if index < self.position or index > self.position + 30:
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            self.position = index
        image = None
        while self.position <= index:
            ok, image = self.cap.read()
            if not ok:
                return None
            self.cache[self.position] = image
            self.position += 1
            while len(self.cache) > self.keep:
                self.cache.popitem(last=False)
        return image

    def close(self) -> None:
        self.cap.release()


COLORS = {"in": (90, 220, 90), "out": (60, 160, 255), "line": (255, 210, 0), "zone": (220, 120, 255),
          "active": (0, 255, 255), "text": (240, 240, 240)}


def render(
    session: LabelSession, image: np.ndarray, show_help: bool, playing: bool, speed: float
) -> np.ndarray:
    """Draw lines, zones, the timeline and the help text on a copy of the frame."""
    canvas = image.copy()
    h, w = canvas.shape[:2]
    active = session.active_target()

    def px(p) -> tuple[int, int]:
        return round(p[0] * w), round(p[1] * h)

    for zone in session.labels.zones:
        is_active = active == ("zone", zone.name)
        pts = np.array([px(p) for p in zone.polygon], np.int32)
        cv2.polylines(canvas, [pts], True, COLORS["active" if is_active else "zone"], 2 if is_active else 1)
        cv2.putText(canvas, zone.name, pts[0] + (4, 18), 0, 0.6, COLORS["zone"], 2)
    for line in session.labels.lines:
        is_active = active == ("line", line.name)
        a, b = px(line.p1), px(line.p2)
        cv2.line(canvas, a, b, COLORS["active" if is_active else "line"], 3 if is_active else 2)
        # arrow from the middle towards the IN side
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        dx, dy = b[0] - a[0], b[1] - a[1]
        length = max((dx * dx + dy * dy) ** 0.5, 1e-6)
        nx, ny = -dy / length, dx / length  # right-hand side on screen (y down) is (-dy, dx)
        if line.in_direction == "to_left":
            nx, ny = -nx, -ny
        tip = (round(mx + nx * 40), round(my + ny * 40))
        cv2.arrowedLine(canvas, (round(mx), round(my)), tip, COLORS["in"], 2, tipLength=0.35)
        cv2.putText(canvas, f"{line.name} (IN)", (tip[0] + 4, tip[1]), 0, 0.55, COLORS["in"], 2)
    for p in session.pending:
        cv2.circle(canvas, px(p), 5, COLORS["active"], -1)

    # timeline with marks
    bar_y = h - 14
    cv2.rectangle(canvas, (0, bar_y - 8), (w, h), (25, 25, 25), -1)
    total = max(session.n_frames / session.fps, 1e-6)
    for c in session.labels.crossings:
        x = round(c.t / total * (w - 1))
        cv2.line(canvas, (x, bar_y - 6), (x, h), COLORS[c.dir], 2)
    for o in session.labels.occupancy:
        x = round(o.t / total * (w - 1))
        cv2.circle(canvas, (x, bar_y + 3), 2, COLORS["text"], -1)
    cx = round(session.t / total * (w - 1))
    cv2.line(canvas, (cx, bar_y - 8), (cx, h), COLORS["active"], 1)

    # status
    near = [c for c in session.labels.crossings if abs(c.t - session.t) <= 1.0]
    status = (f"{session.t:7.2f}s  frame {session.frame}/{session.n_frames - 1}  "
              f"{'PLAY' if playing else 'PAUSE'} x{speed:g}  class: {session.cls}  "
              f"active: {active[1] if active else '-'}  tool: {session.tool}")
    lines = [status, session.summary()]
    if near:
        lines.append("near: " + ", ".join(f"{c.dir.upper()} {c.cls} {c.t:.2f}s" for c in near))
    if session.message:
        lines.append(session.message)
    if show_help:
        lines += HELP
    y = 22
    for text in lines:
        cv2.putText(canvas, text, (8, y), 0, 0.5, (0, 0, 0), 4)
        cv2.putText(canvas, text, (8, y), 0, 0.5, COLORS["text"], 1)
        y += 20
    return canvas


def run_gui(session: LabelSession, video: str | Path, max_width: int = 1280) -> LabelSession:
    """Open the labeling window. Returns when the user presses ESC or closes the window."""
    reader = _FrameReader(video)
    window = "CountVision count-helper (H = help, ESC = quit)"
    try:
        cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    except cv2.error as exc:
        raise CountVisionError(
            "Cannot open a window. Install opencv-python (not -headless) and run this on a "
            "computer with a screen."
        ) from exc
    state = {"scale": 1.0}

    def on_mouse(event, x, y, flags, param) -> None:  # noqa: ARG001
        shown = state.get("size")
        if not shown:
            return
        nx, ny = x / shown[0], y / shown[1]
        if event == cv2.EVENT_LBUTTONDOWN:
            session.click((nx, ny))
        elif event == cv2.EVENT_RBUTTONDOWN:
            session.close_zone()

    cv2.setMouseCallback(window, on_mouse)
    playing, speed, show_help = False, 1.0, True
    speeds = [0.25, 0.5, 1.0, 2.0, 4.0]
    try:
        while True:
            image = reader.get(session.frame)
            if image is None:
                playing = False
                session.seek(session.frame - 1)
                image = reader.get(session.frame)
                if image is None:
                    break
            scale = min(1.0, max_width / image.shape[1])
            if scale < 1.0:
                image = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            state["size"] = (image.shape[1], image.shape[0])
            cv2.imshow(window, render(session, image, show_help, playing, speed))
            delay = max(1, round(1000 / (session.fps * speed))) if playing else 30
            code = cv2.waitKeyEx(delay)
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                break
            if code == -1:
                if playing:
                    if session.frame >= session.n_frames - 1:
                        playing = False
                    else:
                        session.seek(session.frame + 1)
                continue
            key = ARROWS.get(code) or chr(code & 0xFF).lower()
            if code & 0xFF == 27:
                break
            if key == " ":
                playing = not playing
            elif key == "h":
                show_help = not show_help
            elif key == "[":
                speed = speeds[max(0, speeds.index(speed) - 1)]
            elif key == "]":
                speed = speeds[min(len(speeds) - 1, speeds.index(speed) + 1)]
            elif key == "w":
                session.save()
                session.message = f"Saved {session.path}"
            elif key in ("\r", "\n"):
                session.close_zone()
            else:
                if key in ("i", "o", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9"):
                    playing = False  # stop so the mark matches what you see
                session.handle_key(key)
    finally:
        reader.close()
        cv2.destroyAllWindows()
        session.save()
    return session
