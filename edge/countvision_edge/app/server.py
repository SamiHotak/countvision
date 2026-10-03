"""FastAPI server for the local web app.

    countvision-edge app --config configs/local.yaml

Security model (local app, phase 1):
* By default the server listens on 127.0.0.1 only, so nobody else on the network can see it,
  and the Host header must be localhost (protects against DNS-rebinding web pages).
* With ``--host 0.0.0.0`` (to open it on a phone) a random access key is required. The key is
  printed in the console as part of the URL and then kept in a cookie.
* Every request that changes something needs the header ``X-CountVision: 1``. Browsers do not
  let other web pages send that header to us, so a random website cannot change the lines.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import secrets
import tempfile
import threading
import time
import zipfile
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .. import __version__
from ..config import SourceConfig
from ..errors import ConfigError
from ..report import build_report, export_csv
from ..viz import ACCENT, CLASS_COLORS, DEFAULT_CLASS_COLOR
from .runner import LiveRunner
from .stats import hourly_csv, line_timeline, line_totals, report_text, zone_totals

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
VIDEO_EXTENSIONS = {".mp4", ".avi", ".mov", ".mkv", ".m4v", ".webm", ".mpg", ".mpeg", ".ts", ".wmv"}
MAX_UPLOAD_BYTES = 4 * 1024**3  # 4 GB
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
WRITE_HEADER = "x-countvision"
KEY_COOKIE = "cv_key"


def _hex(bgr: tuple[int, int, int]) -> str:
    b, g, r = bgr
    return f"#{r:02x}{g:02x}{b:02x}"


# ------------------------------------------------------------------------------ request models


class LineIn(BaseModel):
    name: str
    p1: list[float] = Field(min_length=2, max_length=2)
    p2: list[float] = Field(min_length=2, max_length=2)
    in_direction: str = "to_right"


class ZoneIn(BaseModel):
    name: str
    kind: str = "area"
    polygon: list[list[float]]


class GeometryIn(BaseModel):
    lines: list[LineIn] = Field(default_factory=list, max_length=50)
    zones: list[ZoneIn] = Field(default_factory=list, max_length=50)


class SourceIn(BaseModel):
    kind: str = Field(pattern="^(config|webcam)$")
    index: int | None = Field(None, ge=0, le=20)


# ------------------------------------------------------------------------------ app factory


def create_app(runner: LiveRunner, *, access_key: str | None = None,
               trusted_hosts: set[str] | None = None, autostart: bool = True,
               stopping: threading.Event | None = None) -> FastAPI:
    """Build the web app around a runner.

    ``access_key``: required on every request (as cookie or ``?key=``) when not None.
    ``trusted_hosts``: allowed Host header names (None = any, used with an access key).
    ``stopping``: set when the server begins to shut down, so the endless preview and live
    streams end at once (otherwise Ctrl+C waits for open browser tabs).
    """
    closing = stopping or threading.Event()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if autostart and runner.run is None:
            await run_in_threadpool(runner.start)
        yield
        closing.set()
        await run_in_threadpool(runner.stop)

    app = FastAPI(title="CountVision local", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runner = runner

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if request.headers.get("host", "").startswith("["):  # IPv6 literal
            host = request.headers["host"].split("]")[0] + "]"
        if trusted_hosts is not None and host not in trusted_hosts:
            return JSONResponse({"detail": "Unknown host name."}, status_code=400)
        if access_key is not None:
            given = request.cookies.get(KEY_COOKIE) or request.query_params.get("key") or ""
            if not secrets.compare_digest(given, access_key):
                return JSONResponse(
                    {"detail": "Access key missing. Open the full link shown in the console."},
                    status_code=401,
                )
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(WRITE_HEADER) != "1":
            return JSONResponse({"detail": "Missing X-CountVision header."}, status_code=403)
        response = await call_next(request)
        if access_key is not None and request.query_params.get("key") == access_key:
            response.set_cookie(KEY_COOKIE, access_key, httponly=True, samesite="strict")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    # -- read ---------------------------------------------------------------------------

    @app.get("/api/meta")
    async def meta() -> dict:
        return {
            "version": __version__,
            "accent": _hex(ACCENT),
            "class_colors": {name: _hex(color) for name, color in CLASS_COLORS.items()},
            "default_class_color": _hex(DEFAULT_CLASS_COLOR),
        }

    @app.get("/api/status")
    async def status() -> dict:
        return runner.status()

    @app.get("/api/geometry")
    async def get_geometry() -> dict:
        return runner.geometry()

    def totals_payload() -> dict:
        scope = runner.scope()
        lines = line_totals(runner.buffer, scope.camera_id, scope.start, scope.end)
        zones = zone_totals(runner.buffer, scope.camera_id, scope.start, scope.end)
        for line in runner.base_cam.lines:
            lines.setdefault(line.name, {"in": 0, "out": 0, "by_class": {}})
        for zone in runner.base_cam.zones:
            zones.setdefault(
                zone.name, {"visits": 0, "dwell_avg_s": 0.0, "dwell_max_s": 0.0, "occupancy_max": 0}
            )
        return {"scope": scope.as_dict(), "lines": lines, "zones": zones}

    @app.get("/api/totals")
    async def totals() -> dict:
        return await run_in_threadpool(totals_payload)

    @app.get("/api/timeline")
    async def timeline(line: str | None = Query(None, max_length=64)) -> dict:
        scope = runner.scope()
        buckets = await run_in_threadpool(
            line_timeline, runner.buffer, scope.camera_id, scope.start, scope.end, scope.bucket_s, line
        )
        return {"scope": scope.as_dict(), "line": line, "buckets": buckets}

    @app.get("/api/report")
    async def report() -> dict:
        scope = runner.scope()
        names = [line.name for line in runner.base_cam.lines]

        def build() -> dict:
            data = build_report(runner.buffer, scope.camera_id, scope.start, scope.end, line_names=names)
            current = totals_payload()
            text = report_text(scope.kind, scope.camera_id, scope.start, scope.end,
                               current["lines"], current["zones"], data)
            return {"scope": scope.as_dict(), "report": data, "totals": current, "text": text}

        return await run_in_threadpool(build)

    @app.get("/api/export.csv")
    async def export_hourly(delimiter: str = Query(",", pattern="^[,;]$")) -> Response:
        scope = runner.scope()
        text = await run_in_threadpool(
            hourly_csv, runner.buffer, scope.camera_id, scope.start, scope.end, delimiter
        )
        stamp = datetime.fromtimestamp(scope.start).strftime("%Y-%m-%d")
        name = f"countvision_{scope.camera_id}_{stamp}_hourly.csv"
        return Response(
            "﻿" + text,  # BOM: Excel then reads the file as UTF-8
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/export.zip")
    async def export_raw() -> Response:
        scope = runner.scope()

        def build() -> bytes:
            data = io.BytesIO()
            cam, start, end = scope.camera_id, scope.start, scope.end
            with tempfile.TemporaryDirectory() as folder, zipfile.ZipFile(
                data, "w", zipfile.ZIP_DEFLATED
            ) as zf:
                zf.writestr("hourly.csv", hourly_csv(runner.buffer, cam, start, end))
                for path in export_csv(runner.buffer, folder, cam, start, end):
                    zf.write(path, path.name)
            return data.getvalue()

        payload = await run_in_threadpool(build)
        stamp = datetime.fromtimestamp(scope.start).strftime("%Y-%m-%d")
        name = f"countvision_{scope.camera_id}_{stamp}.zip"
        return Response(payload, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    # -- preview ------------------------------------------------------------------------

    @app.get("/api/preview.jpg")
    async def preview_jpg() -> Response:
        if runner.preview == "off":
            raise HTTPException(404, "The preview is switched off (--preview off).")
        runner.touch_viewer()
        _, jpeg = runner.latest_jpeg()
        if jpeg is None:
            for _ in range(40):  # wait up to 2 s for the first picture
                await asyncio.sleep(0.05)
                runner.touch_viewer()
                _, jpeg = runner.latest_jpeg()
                if jpeg is not None:
                    break
        if jpeg is None:
            raise HTTPException(503, "No picture yet.")
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/preview.mjpg")
    async def preview_stream(request: Request) -> StreamingResponse:
        if runner.preview == "off":
            raise HTTPException(404, "The preview is switched off (--preview off).")

        async def frames():
            last = -1
            checked = time.monotonic()
            while not closing.is_set():
                runner.touch_viewer()
                seq, jpeg = runner.latest_jpeg()
                if jpeg is not None and seq != last:
                    last = seq
                    yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                           + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n")
                if time.monotonic() - checked > 1.0:
                    checked = time.monotonic()
                    if await request.is_disconnected():
                        break
                await asyncio.sleep(0.03)

        return StreamingResponse(
            frames(), media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/live")
    async def live(request: Request) -> StreamingResponse:
        """Server-sent events: status and totals twice per second."""

        async def events():
            yield "retry: 2000\n\n"
            while not closing.is_set():
                if await request.is_disconnected():
                    break
                payload = {"status": runner.status(), "totals": await run_in_threadpool(totals_payload)}
                yield f"data: {json.dumps(payload)}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # -- write --------------------------------------------------------------------------

    @app.put("/api/geometry")
    async def put_geometry(body: GeometryIn) -> dict:
        try:
            return await run_in_threadpool(
                runner.apply_geometry,
                [line.model_dump() for line in body.lines],
                [zone.model_dump() for zone in body.zones],
            )
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/source")
    async def set_source(body: SourceIn) -> dict:
        if body.kind == "config":
            await run_in_threadpool(runner.start)
        else:
            if body.index is None:
                raise HTTPException(422, "Give the webcam number (0, 1, 2 ...).")
            base = runner.base_cam.source
            data = {"uri": str(body.index), "kind": "webcam"}
            if base.kind == "webcam":  # keep resolution and fourcc from the config
                data.update(width=base.width, height=base.height, fps=base.fps, fourcc=base.fourcc)
            await run_in_threadpool(runner.start, SourceConfig.model_validate(data))
        return runner.status()

    @app.post("/api/stop")
    async def stop() -> dict:
        await run_in_threadpool(runner.stop)
        return runner.status()

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), realtime: bool = Query(False)) -> dict:
        original = Path(file.filename or "video").name
        suffix = Path(original).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise HTTPException(415, f"Unsupported file type '{suffix}'. Use MP4, MOV, AVI or MKV.")
        folder = Path(runner.cfg.data_dir) / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(original).stem)[:60] or "video"
        target = folder / f"{int(time.time())}_{safe}{suffix}"
        size = 0
        try:
            with target.open("wb") as handle:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "The video is larger than 4 GB.")
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        source = SourceConfig(uri=str(target), kind="file", realtime=realtime)
        await run_in_threadpool(runner.start, source, upload=target, label=original)
        return runner.status()

    return app


def make_access_key() -> str:
    return secrets.token_urlsafe(16)
