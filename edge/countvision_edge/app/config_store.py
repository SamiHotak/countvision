"""Save lines and zones drawn in the browser back into the YAML config file.

Only the ``lines``, ``zones`` and ``coordinates`` keys of one camera are changed. Everything
else - comments, ``${ENV}`` placeholders, the order of keys - stays as the user wrote it
(ruamel.yaml round-trip). Before the first change a copy is kept as ``<file>.bak``.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

from ..config import CameraConfig, LineConfig, ZoneConfig
from ..errors import ConfigError

log = logging.getLogger(__name__)


def _point(values) -> CommentedSeq:
    seq = CommentedSeq([round(float(v), 4) for v in values])
    seq.fa.set_flow_style()
    return seq


def _line_yaml(line: LineConfig) -> CommentedMap:
    data = CommentedMap()
    data["name"] = line.name
    data["p1"] = _point(line.p1)
    data["p2"] = _point(line.p2)
    data["in_direction"] = line.in_direction
    extra = line.model_dump(exclude_defaults=True, exclude={"name", "p1", "p2", "in_direction"})
    for key, value in extra.items():
        data[key] = value
    return data


def _zone_yaml(zone: ZoneConfig) -> CommentedMap:
    data = CommentedMap()
    data["name"] = zone.name
    data["kind"] = zone.kind
    polygon = CommentedSeq([_point(p) for p in zone.polygon])
    polygon.fa.set_flow_style()
    data["polygon"] = polygon
    extra = zone.model_dump(exclude_defaults=True, exclude={"name", "kind", "polygon"})
    for key, value in extra.items():
        data[key] = value
    return data


def save_geometry(config_path: str | Path, camera: CameraConfig) -> Path:
    """Write the camera's lines and zones (and coordinate mode) into the config file."""
    path = Path(config_path)
    yaml = YAML()  # round-trip mode keeps comments
    yaml.preserve_quotes = True
    yaml.width = 120
    try:
        document = yaml.load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - any read/parse problem means: do not write
        raise ConfigError(f"Could not read {path} to save the lines: {exc}") from exc
    cameras = document.get("cameras") if isinstance(document, dict) else None
    target = None
    for item in cameras or []:
        if isinstance(item, dict) and str(item.get("id", "cam1")) == camera.id:
            target = item
            break
    if target is None:
        raise ConfigError(f"Camera '{camera.id}' not found in {path}")

    target["coordinates"] = camera.coordinates
    target["lines"] = CommentedSeq([_line_yaml(line) for line in camera.lines])
    target["zones"] = CommentedSeq([_zone_yaml(zone) for zone in camera.zones])
    if camera.speed is not None and "speed" in target:
        target["speed"]["p1"] = _point(camera.speed.p1)
        target["speed"]["p2"] = _point(camera.speed.p2)

    backup = path.with_name(path.name + ".bak")
    if not backup.exists():
        shutil.copy2(path, backup)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        yaml.dump(document, handle)
    temp.replace(path)  # atomic on the same disk: a crash never leaves half a file
    log.info("Saved %d lines and %d zones to %s", len(camera.lines), len(camera.zones), path)
    return path
