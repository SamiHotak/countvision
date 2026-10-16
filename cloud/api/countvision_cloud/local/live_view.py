"""Live camera picture of CountVision Local (the cloud edition never shows video).

The built-in edge agent serves the camera pictures with the analysis drawn on them on
127.0.0.1:<local_live_port> (countvision_edge.live_view), protected by a key. The key is derived
from this installation's secret key, so it is never stored anywhere else than in edge.yaml (written
by setup.py). This module decides WHO may watch WHAT and passes the picture on:

* only members of the business that is logged in (any role; the owner can limit it to admins),
* only cameras of the built-in device "This computer" (the API reaches only that agent),
* in the mode the owner chose for the installation: "blur" (default: people and vehicles
  pixelated), "full" (the picture as it is) or "off" (no live picture at all),
* nothing is recorded or stored: the JPEGs only pass through memory.

Watching is written to the activity log (at most once per person, camera and 30 minutes), so the
owner can show who looked at the cameras (GDPR accountability).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from sqlalchemy.orm import Session

from ..product.models import Camera
from ..product.services import camera_config
from ..redis_client import get_redis
from ..saas.errors import AppError, Conflict, Forbidden
from ..saas.models import AppSetting, Organization, Role, User
from ..saas.services import audit
from ..settings import get_settings

log = logging.getLogger(__name__)

KEY_SETTING = "live_view"
Mode = Literal["full", "blur", "off"]
MODES: tuple[str, ...] = ("full", "blur", "off")
DEFAULT: dict[str, Any] = {"mode": "blur", "admins_only": False}
WATCH_LOG_S = 30 * 60
_ROLE_ORDER = [Role.VIEWER, Role.MEMBER, Role.ADMIN, Role.OWNER]


def edge_key() -> str:
    """The key the API and the built-in edge agent share (from the installation's secret key)."""
    secret = get_settings().secret_key.encode()
    return hmac.new(secret, b"countvision-live-view-v1", hashlib.sha256).hexdigest()[:48]


def edge_url() -> str:
    return f"http://127.0.0.1:{get_settings().local_live_port}"


# --- the owner's setting ------------------------------------------------------------------------------


def current(db: Session) -> dict[str, Any]:
    row = db.get(AppSetting, KEY_SETTING)
    value = {**DEFAULT, **(dict(row.value) if row else {})}
    if value["mode"] not in MODES:
        value["mode"] = DEFAULT["mode"]
    return value


def can_watch(setting: dict[str, Any], role: Role) -> bool:
    if setting["mode"] == "off":
        return False
    if setting["admins_only"]:
        return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(Role.ADMIN)
    return True


def summary(db: Session, role: Role) -> dict[str, Any]:
    setting = current(db)
    return {**setting, "can_watch": can_watch(setting, role), "can_change": role == Role.OWNER}


def change(db: Session, org: Organization, actor: User, *, mode: str, admins_only: bool) -> dict[str, Any]:
    """Owner only (checked by the router). Audited with the old and the new value."""
    if mode not in MODES:
        raise AppError("Choose full, blur or off.", code="validation", status=422)
    old = current(db)
    new = {"mode": mode, "admins_only": bool(admins_only)}
    if old == new:
        return new
    row = db.get(AppSetting, KEY_SETTING)
    if row is None:
        db.add(AppSetting(key=KEY_SETTING, value=new))
    else:
        row.value = new
    audit.record(db, "privacy.live_view_changed", actor=actor.id, org_id=org.id, target_type="org",
                 target_id=org.id, old_mode=old["mode"], mode=new["mode"],
                 old_admins_only=old["admins_only"], admins_only=new["admins_only"])
    db.flush()
    return new


# --- one picture or a stream ------------------------------------------------------------------------


@dataclass
class Target:
    """What the edge agent must be asked for."""

    url: str
    mode: str
    edge_camera_id: str


def target(db: Session, org: Organization, user: User, role: Role, camera_id: uuid.UUID, *,
           kind: Literal["frame.jpg", "stream.mjpg"], geometry: bool, width: int, fps: float) -> Target:
    """Check everything, then build the edge agent's address. Raises a clear error otherwise."""
    setting = current(db)
    if setting["mode"] == "off":
        raise Forbidden("The live picture is switched off. The owner can switch it on in Settings -> "
                        "Privacy.", code="live_off")
    if not can_watch(setting, role):
        raise Forbidden("Only admins and the owner may watch the live picture.", code="live_admins_only")
    cam: Camera = camera_config.get_camera(db, org, camera_id)
    if cam.removed_at is not None:
        raise Conflict("This camera was removed.", code="camera_removed")
    if not cam.device.builtin:
        raise Conflict("The live picture is only available for cameras of this computer.",
                       code="live_not_here")
    if cam.plan_paused:
        raise Conflict("This camera is not running: it is over your licence's camera limit.",
                       code="camera_not_running")
    if kind == "stream.mjpg":
        _log_watch(db, org, user, cam, setting["mode"])
    query = f"mode={setting['mode']}&geometry={1 if geometry else 0}&width={width}&fps={fps:g}"
    return Target(f"{edge_url()}/cameras/{cam.edge_camera_id}/{kind}?{query}", setting["mode"],
                  cam.edge_camera_id)


def _log_watch(db: Session, org: Organization, user: User, cam: Camera, mode: str) -> None:
    marker = f"cv:live:watch:{user.id}:{cam.id}"
    try:
        first = get_redis().set(marker, "1", ex=WATCH_LOG_S, nx=True)
    except Exception:  # noqa: BLE001 - the log entry is a nice-to-have, the picture is not blocked
        first = True
    if first:
        audit.record(db, "camera.live_viewed", actor=user.id, org_id=org.id, target_type="camera",
                     target_id=cam.id, name=cam.name, mode=mode)


def _edge_error(status: int, body: bytes) -> AppError:
    import json

    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        err = {}
    code = err.get("code", "")
    if code in ("no_picture",):
        return Conflict("The camera has no picture right now (starting, offline or reconnecting).",
                        code="no_picture")
    if code == "camera_not_running" or status == 404:
        return Conflict("This camera is not running on this computer yet. Wait a few seconds.",
                        code="camera_not_running")
    if status == 401:
        return AppError("The counting service did not accept the live picture key. Restart CountVision.",
                        code="live_key", status=502)
    return AppError(f"The counting service could not make a picture ({err.get('message') or status}).",
                    code="live_error", status=502)


def _client(timeout) -> httpx.AsyncClient:
    """Plain HTTP to 127.0.0.1: no proxy settings from the environment (a shop PC's proxy must not
    see the camera picture) and no TLS setup."""
    return httpx.AsyncClient(timeout=timeout, trust_env=False, verify=False)  # noqa: S501 - http only


_UNREACHABLE = ("The counting service on this computer is not running (it starts after the setup "
                "wizard, and restarts by itself). Try again in a few seconds.")


async def fetch_frame(t: Target) -> bytes:
    headers = {"Authorization": f"Bearer {edge_key()}"}
    try:
        async with _client(timeout=10) as client:
            resp = await client.get(t.url, headers=headers)
    except httpx.HTTPError as exc:
        raise AppError(_UNREACHABLE, code="edge_unreachable", status=503) from exc
    if resp.status_code != 200:
        raise _edge_error(resp.status_code, resp.content)
    return resp.content


class Stream:
    """An open MJPEG stream from the edge agent. ``open()`` first (errors become clear API errors
    BEFORE the response starts), then iterate ``body()``; it is closed when the viewer leaves."""

    def __init__(self, t: Target) -> None:
        self.target = t
        self.content_type = "multipart/x-mixed-replace"
        self._client: httpx.AsyncClient | None = None
        self._resp: httpx.Response | None = None

    async def open(self) -> None:
        self._client = _client(timeout=httpx.Timeout(10, read=30))
        try:
            req = self._client.build_request("GET", self.target.url,
                                             headers={"Authorization": f"Bearer {edge_key()}"})
            self._resp = await self._client.send(req, stream=True)
        except httpx.HTTPError as exc:
            await self.close()
            raise AppError(_UNREACHABLE, code="edge_unreachable", status=503) from exc
        if self._resp.status_code != 200:
            body = await self._resp.aread()
            status = self._resp.status_code
            await self.close()
            raise _edge_error(status, body)
        self.content_type = self._resp.headers.get("content-type", self.content_type)

    async def body(self) -> AsyncIterator[bytes]:
        assert self._resp is not None
        try:
            async for chunk in self._resp.aiter_raw():
                yield chunk
        except httpx.HTTPError:
            pass  # the agent stopped (camera removed, restart): the browser reconnects
        finally:
            await self.close()

    async def close(self) -> None:
        if self._resp is not None:
            await self._resp.aclose()
            self._resp = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
