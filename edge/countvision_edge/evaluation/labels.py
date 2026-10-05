"""Hand labels for evaluation clips (one YAML file per clip).

A label file says WHERE to count (lines and zones, normalised 0..1 coordinates) and WHAT
really happened (the ground truth): every line crossing with its time and direction, and
occupancy samples ("at 12.0 s there were 3 people in zone 'aisle'").

The file is written by ``countvision-edge count-helper`` and can be edited by hand.
Videos are NOT stored in git. ``download_url`` is used by ``countvision-edge eval fetch``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..config import LineConfig, ZoneConfig
from ..errors import ConfigError

Split = Literal["tune", "test", "dev"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GtCrossing(_Model):
    """One real line crossing. ``t`` = seconds from the start of the video, at the moment the
    anchor point (feet or box centre, see ``anchor``) crosses the line."""

    t: float = Field(ge=0)
    line: str
    dir: Literal["in", "out"]
    cls: str = "person"


class GtOccupancy(_Model):
    """How many objects were really inside ``zone`` at time ``t``."""

    t: float = Field(ge=0)
    zone: str
    n: int = Field(ge=0)
    cls: str | None = None  # None = all classes of the clip


class ClipLabels(_Model):
    """Everything known about one evaluation clip."""

    clip: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    video: str  # file name inside the videos folder
    title: str | None = None
    source_url: str | None = None  # where a human can see the clip and its licence
    download_url: str | None = None  # direct file link (used by "eval fetch")
    license: str | None = None
    credit: str | None = None  # attribution text (needed for CC BY)
    split: Split = "dev"
    scene: Literal["clear", "hard"] = "clear"
    tags: list[str] = Field(default_factory=list)
    labeled_by: str | None = None
    verified_by: str | None = None  # second person who checked the labels
    classes: list[str] = Field(default_factory=lambda: ["person"], min_length=1)
    anchor: Literal["bottom_center", "center"] = "bottom_center"
    labeled_until_s: float | None = Field(None, gt=0)  # None = the whole video is labeled
    lines: list[LineConfig] = Field(default_factory=list)
    zones: list[ZoneConfig] = Field(default_factory=list)
    crossings: list[GtCrossing] = Field(default_factory=list)
    occupancy: list[GtOccupancy] = Field(default_factory=list)
    notes: str = ""

    @model_validator(mode="after")
    def _check(self) -> ClipLabels:
        line_names = {line.name for line in self.lines}
        zone_names = {zone.name for zone in self.zones}
        if len(line_names) != len(self.lines) or len(zone_names) != len(self.zones):
            raise ValueError(f"clip '{self.clip}': line and zone names must be unique")
        for c in self.crossings:
            if c.line not in line_names:
                raise ValueError(f"clip '{self.clip}': crossing at {c.t}s uses unknown line '{c.line}'")
            if c.cls not in self.classes:
                raise ValueError(f"clip '{self.clip}': crossing at {c.t}s has class '{c.cls}' "
                                 f"that is not in classes {self.classes}")
        for o in self.occupancy:
            if o.zone not in zone_names:
                raise ValueError(f"clip '{self.clip}': occupancy at {o.t}s uses unknown zone '{o.zone}'")
        for item in [*self.lines, *self.zones]:
            points = [item.p1, item.p2] if isinstance(item, LineConfig) else list(item.polygon)
            for x, y in points:
                if not (-0.01 <= x <= 1.01 and -0.01 <= y <= 1.01):
                    raise ValueError(f"clip '{self.clip}': '{item.name}' has a point outside 0..1. "
                                     "Label files use normalised coordinates.")
        self.crossings.sort(key=lambda c: c.t)
        self.occupancy.sort(key=lambda o: (o.zone, o.t))
        return self

    def in_labeled_range(self, t: float) -> bool:
        return self.labeled_until_s is None or t <= self.labeled_until_s


def load_labels(path: str | Path) -> ClipLabels:
    """Read and validate one label file."""
    file = Path(path)
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        return ClipLabels.model_validate(data)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise ConfigError(f"Bad label file {file}: {exc}") from exc


def load_label_dir(folder: str | Path, splits: list[str] | None = None) -> list[ClipLabels]:
    """All ``*.yaml`` label files in a folder, sorted by clip id, optionally only some splits."""
    root = Path(folder)
    if not root.is_dir():
        raise ConfigError(f"Label folder not found: {root}")
    clips = [load_labels(p) for p in sorted(root.glob("*.yaml"))]
    ids = [c.clip for c in clips]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate clip ids in {root}: {', '.join(sorted(duplicates))}")
    if splits:
        clips = [c for c in clips if c.split in splits]
    return sorted(clips, key=lambda c: c.clip)


def labels_to_yaml(labels: ClipLabels) -> str:
    """Readable YAML: short flow-style rows for crossings and occupancy."""
    data = labels.model_dump(mode="json", exclude_none=True)
    for line in data.get("lines", []):
        _drop_defaults(line, LineConfig)
    for zone in data.get("zones", []):
        _drop_defaults(zone, ZoneConfig)
    head = {k: v for k, v in data.items() if k not in ("crossings", "occupancy", "lines", "zones")}
    text = yaml.safe_dump(head, sort_keys=False, allow_unicode=True, width=100)
    text += yaml.safe_dump({"lines": data.get("lines", [])}, sort_keys=False, default_flow_style=None)
    text += yaml.safe_dump({"zones": data.get("zones", [])}, sort_keys=False, default_flow_style=None)
    text += "crossings:" + ("\n" if labels.crossings else " []\n")
    for c in labels.crossings:
        text += f"  - {{t: {c.t:.2f}, line: {_q(c.line)}, dir: {c.dir}, cls: {_q(c.cls)}}}\n"
    text += "occupancy:" + ("\n" if labels.occupancy else " []\n")
    for o in labels.occupancy:
        extra = f", cls: {_q(o.cls)}" if o.cls else ""
        text += f"  - {{t: {o.t:.2f}, zone: {_q(o.zone)}, n: {o.n}{extra}}}\n"
    return text


def save_labels(labels: ClipLabels, path: str | Path) -> Path:
    """Write a label file (validated again before writing). Writes to a temp file first."""
    ClipLabels.model_validate(labels.model_dump())
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(out.suffix + ".tmp")
    tmp.write_text(labels_to_yaml(labels), encoding="utf-8")
    tmp.replace(out)
    return out


def _q(value: str) -> str:
    return value if value.replace("_", "").replace("-", "").isalnum() else repr(value)


def _drop_defaults(item: dict, model: type[BaseModel]) -> None:
    for name, field in model.model_fields.items():
        if name in item and field.default is not None and item[name] == field.default:
            del item[name]
