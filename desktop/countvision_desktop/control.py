"""Only one CountVision per computer. Starting it a second time (desktop icon, Start menu, autostart)
brings the running one's window to the front instead.

The running app listens on 127.0.0.1 (a random port) for "show" and "quit", protected by a random
token. Port and token are in ``home/run/app.lock``.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import secrets
import threading
import urllib.error
import urllib.request
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

log = logging.getLogger(__name__)

LOCK_FILE = "app.lock"


def _call(lock: dict, action: str, timeout: float = 3.0) -> dict | None:
    req = urllib.request.Request(f"http://127.0.0.1:{lock['port']}/{action}", method="POST",
                                 headers={"X-Token": str(lock["token"])}, data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except (OSError, ValueError, urllib.error.URLError):
        return None


def running_instance(run_dir: Path) -> dict | None:
    """The lock of a CountVision that runs and answers, else None (a stale lock is ignored)."""
    try:
        lock = json.loads((run_dir / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(lock, dict) or "port" not in lock or "token" not in lock:
        return None
    return lock if _call(lock, "ping") is not None else None


def ask(run_dir: Path, action: str) -> bool:
    """Ask the running CountVision to "show" its window or "quit". False = none is running."""
    lock = running_instance(run_dir)
    return lock is not None and _call(lock, action, timeout=60 if action == "quit" else 3) is not None


class ControlServer:
    def __init__(self, run_dir: Path, actions: dict[str, Callable[[], dict | None]]) -> None:
        self.run_dir = run_dir
        self.actions = {"ping": lambda: {"ok": True}, **actions}
        self.token = secrets.token_urlsafe(24)
        self._httpd: ThreadingHTTPServer | None = None

    def start(self) -> None:
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A002
                pass

            def do_POST(self) -> None:  # noqa: N802
                token = self.headers.get("X-Token", "")
                action = server.actions.get(self.path.strip("/"))
                if not hmac.compare_digest(token.encode(), server.token.encode()) or action is None:
                    self.send_response(403)
                    self.end_headers()
                    return
                try:
                    result = action() or {}
                except Exception as exc:  # noqa: BLE001
                    log.exception("Control action %s failed", self.path)
                    result = {"error": str(exc)}
                body = json.dumps(result).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="control", daemon=True).start()
        self.run_dir.mkdir(parents=True, exist_ok=True)
        lock = self.run_dir / LOCK_FILE
        tmp = lock.with_name(LOCK_FILE + ".tmp")
        tmp.write_text(json.dumps({"pid": os.getpid(), "port": self._httpd.server_address[1], "token": self.token}),
                       encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, lock)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        with contextlib.suppress(OSError):
            lock = json.loads((self.run_dir / LOCK_FILE).read_text(encoding="utf-8"))
            if lock.get("token") == self.token:
                (self.run_dir / LOCK_FILE).unlink()
