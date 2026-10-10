"""Config from the cloud: lines/zones/classes/schedule edited in the web app, and snapshots.

How it works:
* A thread waits in GET /api/device/poll (long-poll, up to 25 s). The cloud answers at once
  when something changed: a new camera config or a snapshot request.
* A new config is checked, applied to the RUNNING camera (from the next frame; line totals of
  lines with the same name are kept) and saved in <data_dir>/cloud_config.json. At the next
  start that file is applied over the YAML config, so the cloud's version stays in force.
  The version is reported back with the next upload (a few seconds later).
* A snapshot request: the newest frame is copied, every detected person/vehicle is pixelated,
  the picture is scaled to max 960 px wide and sent ONCE as JPEG. Nothing is stored here.
  ``privacy.snapshots: false`` in the config refuses all snapshot requests.

Coordinates from the cloud are normalized (0..1). A camera configured with pixel coordinates
is converted when the cloud config is applied.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .cloud import CloudError, Credentials, normalize_url, request_json
from .config import CameraConfig
from .inputs.live_source import Backoff

log = logging.getLogger(__name__)

OVERLAY_FILE = "cloud_config.json"
SNAPSHOT_MAX_WIDTH = 960


# ------------------------------------------------------------------------ overlay file


def overlay_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / OVERLAY_FILE


def load_overlay(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """{camera id: {"version": n, "config": {...}, "timezone": "..."}} saved from the cloud."""
    path = overlay_path(data_dir)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return dict(data.get("cameras", {}))
    except (OSError, ValueError) as exc:
        log.warning("Ignoring damaged %s (%s)", path, exc)
        return {}


def save_overlay(data_dir: str | Path, cameras: dict[str, dict[str, Any]]) -> None:
    path = overlay_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{OVERLAY_FILE}.tmp")
    tmp.write_text(json.dumps({"cameras": cameras, "saved": time.time()}, indent=2), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------- config conversion


def _norm(point, size: tuple[int, int] | None, mode: str) -> list[float]:
    if mode == "normalized":
        return [round(float(point[0]), 5), round(float(point[1]), 5)]
    if not size:
        raise ValueError("pixel coordinates need the frame size")
    return [round(float(point[0]) / size[0], 5), round(float(point[1]) / size[1], 5)]


def report_doc(cam: CameraConfig, frame_size: tuple[int, int] | None) -> dict[str, Any] | None:
    """The camera's current config in the cloud's format (normalized), or None if unknown."""
    try:
        doc: dict[str, Any] = {
            "lines": [{"name": line.name, "p1": _norm(line.p1, frame_size, cam.coordinates),
                       "p2": _norm(line.p2, frame_size, cam.coordinates), "in_direction": line.in_direction}
                      for line in cam.lines],
            "zones": [{"name": z.name, "polygon": [_norm(p, frame_size, cam.coordinates) for p in z.polygon],
                       "kind": z.kind} for z in cam.zones],
            "classes": list(cam.classes),
            "anchor": cam.anchor,
            "schedule": None,
        }
    except ValueError:
        return None  # pixel config and no frame yet
    if cam.schedule is not None:
        doc["schedule"] = {"days": cam.schedule.days, "start": cam.schedule.start, "end": cam.schedule.end}
    return doc


def apply_doc(cam: CameraConfig, doc: dict[str, Any], timezone: str | None,
              frame_size: tuple[int, int] | None = None) -> CameraConfig:
    """A new CameraConfig = the old one with lines, zones, classes, anchor and schedule from the
    cloud. Raises ValueError (pydantic) if the result is not valid."""
    data = cam.model_dump()
    old_zones = {z["name"]: z for z in data.get("zones", [])}
    data["lines"] = [{"name": line["name"], "p1": line["p1"], "p2": line["p2"],
                      "in_direction": line.get("in_direction", "to_right")} for line in doc.get("lines", [])]
    zones = []
    for z in doc.get("zones", []):
        base = dict(old_zones.get(z["name"], {}))  # keep tuning (grace, min visit) of known zones
        base.update(name=z["name"], polygon=z["polygon"], kind=z.get("kind", "area"))
        zones.append(base)
    data["zones"] = zones
    data["classes"] = list(doc.get("classes") or ["person"])
    data["anchor"] = doc.get("anchor", "bottom_center")
    schedule = doc.get("schedule")
    data["schedule"] = dict(schedule, timezone=timezone) if schedule else None
    if cam.coordinates == "pixel":
        data["coordinates"] = "normalized"
        if cam.speed is not None:
            if frame_size:
                data["speed"]["p1"] = _norm(cam.speed.p1, frame_size, "pixel")
                data["speed"]["p2"] = _norm(cam.speed.p2, frame_size, "pixel")
            else:
                log.warning("Camera %s: speed calibration dropped (pixel coordinates, no frame yet)", cam.id)
                data["speed"] = None
    return CameraConfig.model_validate(data)


# ---------------------------------------------------------------------------- the thread


class ConfigSync(threading.Thread):
    """Long-polls the cloud and hands configs / snapshot requests to the supervisor."""

    def __init__(
        self,
        creds: Credentials,
        *,
        apply_config: Callable[[str, int, dict, str | None], str | None],
        take_snapshot: Callable[[str], bytes],
        url: str | None = None,
        wait_s: float = 25.0,
        timeout_s: float = 20.0,
        http: Callable[..., dict] = request_json,
        send_snapshot: Callable[[str, bytes | None, str | None], None] | None = None,
    ) -> None:
        super().__init__(name="cloud-config", daemon=True)
        self.creds = creds
        self.url = normalize_url(url or creds.url)
        self.apply_config = apply_config
        self.take_snapshot = take_snapshot
        self.wait_s = wait_s
        self.timeout_s = timeout_s
        self._http = http
        self._send_snapshot = send_snapshot or self._post_snapshot
        self.stop_event = threading.Event()
        self.rev = -1
        self.applied: dict[str, int] = {}
        self.state = "starting"
        self.last_error: str | None = None
        self._backoff = Backoff(2.0, 2.0, 60.0, jitter=0.2)

    def poll_once(self) -> bool:
        """One long-poll. Returns True when the cloud sent changes."""
        answer = self._http("GET", f"{self.url}/api/device/poll?rev={self.rev}&wait={self.wait_s:g}",
                            token=self.creds.token, timeout=self.wait_s + self.timeout_s)
        self.rev = int(answer.get("rev", self.rev))
        if not answer.get("changed"):
            return False
        for cam in answer.get("cameras", []):
            version = int(cam.get("version", 0))
            if self.applied.get(cam["id"]) == version:
                continue
            error = self.apply_config(cam["id"], version, cam.get("config") or {}, cam.get("timezone"))
            self.applied[cam["id"]] = version
            if error:
                log.error("Cloud config v%d for camera %s NOT applied: %s", version, cam["id"], error)
            else:
                log.info("Cloud config v%d applied to camera %s", version, cam["id"])
        for req in answer.get("snapshots", []):
            self._snapshot(req["request_id"], req["camera_id"])
        return True

    def _snapshot(self, request_id: str, camera_id: str) -> None:
        try:
            image = self.take_snapshot(camera_id)
        except Exception as exc:  # noqa: BLE001 - every failure goes back to the user as text
            log.info("Snapshot for camera %s refused: %s", camera_id, exc)
            self._send_snapshot(request_id, None, str(exc))
            return
        self._send_snapshot(request_id, image, None)
        log.info("Snapshot for camera %s sent (%d kB, people pixelated)", camera_id, len(image) // 1024)

    def _post_snapshot(self, request_id: str, image: bytes | None, error: str | None) -> None:
        import urllib.request

        url = f"{self.url}/api/device/snapshots/{request_id}"
        if image is None:
            data, ctype = json.dumps({"error": error or "failed"}).encode(), "application/json"
        else:
            data, ctype = image, "image/jpeg"
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "Authorization": f"Bearer {self.creds.token}", "Content-Type": ctype})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s):  # noqa: S310
                pass
        except Exception as exc:  # noqa: BLE001
            log.warning("Could not send the snapshot: %s", exc)

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.poll_once()
                self.state, self.last_error = "ok", None
                self._backoff.reset()
            except CloudError as exc:
                self.last_error = str(exc)
                if exc.status == 401:
                    self.state = "revoked"
                    log.error("Cloud config sync stopped: %s", exc)
                    return
                if self.state != "offline":
                    log.info("Cloud config sync: %s (retrying)", exc)
                self.state = "offline"
                self.stop_event.wait(self._backoff.next_delay())
            except Exception as exc:  # noqa: BLE001
                log.exception("Cloud config sync failed")
                self.state, self.last_error = "error", str(exc)
                self.stop_event.wait(self._backoff.next_delay())

    def stop(self) -> None:
        self.stop_event.set()
