"""Connection to the CountVision cloud: pairing and the uploader.

Pairing (once per device):
    countvision-edge pair --url https://app.example.com --code K7QF-3MXP --config site.yaml
saves <data_dir>/cloud.json (cloud address + this device's secret token, file mode 600).

Uploader (inside "countvision-edge run" when paired):
* every ``cloud.upload_interval_s`` it sends what is not yet sent from the local SQLite buffer
  (minute counts, zone statistics, coverage, events), the newest heartbeat per camera and the
  camera states. An upload with no data still tells the cloud "this device is alive".
* Offline? Nothing is lost: rows stay in the buffer with sent=0 and are sent when the network
  is back (several uploads in a row until the backlog is empty). Retry delay 2 s ... 60 s.
* The cloud stores every row with "insert or overwrite", so sending something twice is safe.
* A row that changes during an upload is not marked as sent (see SqliteBuffer.mark_sent_rows).
* Only numbers are sent. Never images, never video, never track coordinates.

HTTP uses only the Python standard library (urllib), so the agent needs no extra package.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import ssl
import stat
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import __version__
from .errors import CountVisionError
from .inputs.live_source import Backoff
from .storage import SqliteBuffer

log = logging.getLogger(__name__)

CREDENTIALS_FILE = "cloud.json"
UPLOAD_TABLES = ("line_counts", "zone_stats", "coverage", "events")
_DROP = ("id", "sent")  # local bookkeeping columns, not sent


class CloudError(CountVisionError):
    """The cloud answered with an error (or could not be reached)."""

    def __init__(self, message: str, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


# ------------------------------------------------------------------------- credentials


@dataclass
class Credentials:
    url: str
    device_id: str
    token: str
    organization: str = ""
    site: str = ""
    device_name: str = ""
    edge_device_id: str = ""
    paired_at: float = field(default_factory=time.time)

    @staticmethod
    def path(data_dir: str | Path) -> Path:
        return Path(data_dir) / CREDENTIALS_FILE

    @classmethod
    def load(cls, data_dir: str | Path) -> Credentials | None:
        path = cls.path(data_dir)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        except (OSError, ValueError, TypeError) as exc:
            raise CountVisionError(f"{path} is damaged ({exc}). Pair the device again.") from exc

    def save(self, data_dir: str | Path) -> Path:
        """Write atomically; only the owner may read it (it contains the device token)."""
        path = self.path(data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{CREDENTIALS_FILE}.tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):  # Windows: no Unix file modes; the folder is the user's
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
        return path


# ------------------------------------------------------------------------------- HTTP


def normalize_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        raise CountVisionError(f"The cloud address must start with http:// or https:// (got {url!r}).")
    return url.removesuffix("/api")


def request_json(method: str, url: str, body: Any = None, *, token: str | None = None,
                 timeout: float = 20.0) -> dict:
    """Send JSON, return JSON. Raises CloudError with the server's message."""
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers = {"Accept": "application/json", "User-Agent": f"countvision-edge/{__version__}"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    context = ssl.create_default_context() if url.startswith("https://") else None
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=context) as resp:  # noqa: S310
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        message, code = f"HTTP {exc.code}", None
        try:
            err = json.loads(exc.read()).get("error", {})
            message, code = err.get("message", message), err.get("code")
        except (ValueError, AttributeError, OSError):
            pass
        raise CloudError(message, status=exc.code, code=code) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise CloudError(f"cannot reach {url.split('/api/')[0]} ({reason})") from exc
    except ValueError as exc:
        raise CloudError(f"the server at {url} did not answer with JSON - is this the right address?"
                         ) from exc


def pair(url: str, code: str, data_dir: str | Path, edge_device_id: str) -> Credentials:
    """Exchange a pairing code for a device token and save it in <data_dir>/cloud.json."""
    base = normalize_url(url)
    answer = request_json("POST", f"{base}/api/device/pair",
                          {"code": code, "edge_device_id": edge_device_id, "agent_version": __version__})
    creds = Credentials(url=base, device_id=answer["device_id"], token=answer["token"],
                        organization=answer.get("org_name", ""), site=answer.get("site_name", ""),
                        device_name=answer.get("device_name", ""), edge_device_id=edge_device_id)
    creds.save(data_dir)
    return creds


# --------------------------------------------------------------------------- uploader


@dataclass
class UploadState:
    """What status.json (and the cloud's device page) shows about uploading."""

    state: str = "starting"  # starting | ok | offline | revoked | error | disabled
    url: str | None = None
    last_success: float | None = None
    last_attempt: float | None = None
    last_error: str | None = None
    pending: int = 0
    uploaded_rows: int = 0
    clock_skew_s: float | None = None


class CloudUploader(threading.Thread):
    """Background thread that empties the buffer into the cloud."""

    def __init__(
        self,
        buffer: SqliteBuffer,
        creds: Credentials,
        *,
        url: str | None = None,
        interval_s: float = 15.0,
        batch_rows: int = 1000,
        timeout_s: float = 20.0,
        status_fn: Callable[[], dict] | None = None,
        send: Callable[[dict], dict] | None = None,
    ) -> None:
        super().__init__(name="cloud-uploader", daemon=True)
        self.buffer = buffer
        self.creds = creds
        self.url = normalize_url(url or creds.url)
        self.interval_s = interval_s
        self.batch_rows = batch_rows
        self.timeout_s = timeout_s
        self.status_fn = status_fn or (lambda: {})
        self._send = send or self._http_send
        self.stop_event = threading.Event()
        self.state = UploadState(url=self.url)
        self._backoff = Backoff(2.0, 2.0, 60.0, jitter=0.2)

    # -- one upload -----------------------------------------------------------------------

    def _http_send(self, payload: dict) -> dict:
        return request_json("POST", f"{self.url}/api/device/ingest", payload, token=self.creds.token,
                            timeout=self.timeout_s)

    def build_batch(self) -> tuple[dict, dict[str, list[dict]], int | None, bool]:
        """(payload, uploaded rows per table, newest heartbeat id, more rows waiting)."""
        rows: dict[str, list[dict]] = {}
        more = False
        for table in UPLOAD_TABLES:
            got = self.buffer.fetch_unsent(table, self.batch_rows)
            rows[table] = got
            more = more or len(got) >= self.batch_rows
        beats = self.buffer.fetch_unsent("heartbeats", 5000)
        newest: dict[str, dict] = {}
        for beat in beats:
            newest[beat["camera_id"]] = beat  # rows come oldest first
        max_beat = max((b["id"] for b in beats), default=None)

        status = dict(self.status_fn() or {})
        cameras = status.pop("cameras", [])
        status["upload"] = {"pending": self.state.pending, "last_success": self.state.last_success,
                            "state": self.state.state}
        payload: dict[str, Any] = {
            "batch_id": uuid.uuid4().hex,
            "sent_at": time.time(),
            "agent_version": __version__,
            "status": status,
            "cameras": cameras,
            "heartbeats": [_clean(b) for b in newest.values()],
        }
        for table, items in rows.items():
            payload[table] = [_clean(r) for r in items]
        return payload, rows, max_beat, more

    def upload_once(self) -> bool:
        """Send one batch. Returns True when more rows are waiting (send again at once)."""
        self.state.last_attempt = time.time()
        payload, rows, max_beat, more = self.build_batch()
        answer = self._send(payload)
        for table, items in rows.items():
            self.buffer.mark_sent_rows(table, items)
        if max_beat is not None:
            self.buffer.mark_sent_upto("heartbeats", max_beat)
        sent = sum(len(v) for v in rows.values())
        self.state.uploaded_rows += sent
        server_time = answer.get("server_time")
        if isinstance(server_time, int | float):
            self.state.clock_skew_s = round(time.time() - server_time, 1)
        if answer.get("rejected"):
            log.warning("Cloud rejected %s rows with impossible times. Is the clock of this computer right?",
                        answer["rejected"])
        return more

    # -- loop -------------------------------------------------------------------------------

    def run(self) -> None:
        log.info("Cloud upload to %s every %.0f s (device %s, site %s)", self.url, self.interval_s,
                 self.creds.device_name or self.creds.device_id, self.creds.site or "?")
        while not self.stop_event.is_set():
            delay = self.interval_s
            try:
                self.state.pending = self.buffer.count_unsent()
                backlog_start = self.state.pending
                more = self.upload_once()
                while more and not self.stop_event.is_set():  # replay a backlog quickly
                    more = self.upload_once()
                    self.stop_event.wait(0.2)
                was = self.state.state
                self.state.pending = self.buffer.count_unsent()
                self.state.state, self.state.last_error = "ok", None
                self.state.last_success = time.time()
                self._backoff.reset()
                if was in ("offline", "error"):
                    log.info("Cloud reachable again, %d waiting rows sent",
                             backlog_start - self.state.pending)
                if self.state.clock_skew_s is not None and abs(self.state.clock_skew_s) > 120:
                    log.warning("This computer's clock is %.0f s off. Turn on automatic time.",
                                self.state.clock_skew_s)
            except CloudError as exc:
                delay = self._on_error(exc)
                if delay is None:
                    return
            except Exception as exc:  # noqa: BLE001 - never kill the counting because of uploads
                log.exception("Cloud upload failed")
                self.state.state, self.state.last_error = "error", f"{type(exc).__name__}: {exc}"
                delay = self._backoff.next_delay()
            self.stop_event.wait(delay)

    def _on_error(self, exc: CloudError) -> float | None:
        """Decide what to do after an error. None = stop uploading."""
        self.state.last_error = str(exc)
        if exc.status == 401:
            self.state.state = "revoked"
            log.error("Cloud: %s Counting continues locally. Pair again with: countvision-edge pair", exc)
            return None
        if exc.status is not None and 400 <= exc.status < 500 and exc.status != 429:
            self.state.state = "error"
            log.error("Cloud refused the upload (%s). Data stays here; retrying in 5 min.", exc)
            return 300.0
        if self.state.state != "offline":
            log.warning("Cloud not reachable: %s. Counting continues; data is kept and sent later.", exc)
        self.state.state = "offline"
        return self._backoff.next_delay()

    def stop(self) -> None:
        self.stop_event.set()

    def flush(self, timeout_s: float = 10.0) -> bool:
        """Last upload on shutdown (best effort). True when everything was sent."""
        deadline = time.monotonic() + timeout_s
        try:
            while time.monotonic() < deadline:
                if not self.upload_once():
                    self.state.pending = self.buffer.count_unsent()
                    return self.state.pending == 0
        except CloudError as exc:
            log.info("Final upload not possible (%s); data stays in the buffer.", exc)
        except Exception:  # noqa: BLE001
            log.exception("Final upload failed")
        return False


def _clean(row: dict) -> dict:
    return {k: v for k, v in row.items() if k not in _DROP}


def make_uploader(cfg, buffer: SqliteBuffer, status_fn: Callable[[], dict]) -> CloudUploader | None:
    """The uploader for a config, or None (not paired / disabled)."""
    if not cfg.cloud.enabled:
        log.info("Cloud upload is disabled in the config (cloud.enabled: false).")
        return None
    creds = Credentials.load(cfg.data_dir)
    if creds is None:
        log.info("Not connected to the cloud (no %s). Counting locally only. To connect: "
                 "countvision-edge pair --url <cloud address> --code <code>", Credentials.path(cfg.data_dir))
        return None
    return CloudUploader(buffer, creds, url=cfg.cloud.url, interval_s=cfg.cloud.upload_interval_s,
                         batch_rows=cfg.cloud.batch_rows, timeout_s=cfg.cloud.timeout_s, status_fn=status_fn)
