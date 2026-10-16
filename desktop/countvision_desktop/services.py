"""One part of CountVision running as a background program: started without a window, its output
in a log file, watched, restarted after a crash (with a growing pause), and stopped cleanly.

Clean stop: the API and the counting service watch a "stop file" (``--stop-file``) and end by
themselves when it appears - that works on Windows, where a program without a console window
cannot be sent Ctrl+C. Whatever has not ended after the grace time is ended hard.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .config import IS_WINDOWS

log = logging.getLogger(__name__)

LOG_MAX_BYTES = 5 * 1024 * 1024
RESTART_DELAYS = (2.0, 5.0, 10.0, 30.0, 60.0)
STABLE_AFTER_S = 120.0  # a part that ran this long starts again with the shortest pause


def no_window_flags() -> dict:
    """Popen arguments: no console window on Windows, own process group everywhere."""
    if IS_WINDOWS:
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
        return {"creationflags": flags}
    return {"start_new_session": True}


def open_log(path: Path):
    """Append to the log file; a file over 5 MB is moved to <name>.1.log first."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, path.with_suffix(".1.log"))
    handle = open(path, "ab", buffering=0)  # noqa: SIM115 - kept open for the child's lifetime
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} =====\n".encode())
    return handle


def run_once(cmd: list[str], *, env: dict[str, str] | None = None, cwd: Path | None = None,
             log_path: Path | None = None, timeout: float = 300) -> int:
    """Run a short command without a window (initdb, migrate ...). Output goes to the log."""
    handle = open_log(log_path) if log_path else subprocess.DEVNULL
    try:
        if log_path:
            handle.write(("$ " + " ".join(str(c) for c in cmd) + "\n").encode())  # type: ignore[union-attr]
        proc = subprocess.run(cmd, env=env, cwd=cwd, stdout=handle, stderr=subprocess.STDOUT,
                              stdin=subprocess.DEVNULL, timeout=timeout, check=False, **no_window_flags())
        return proc.returncode
    finally:
        if log_path:
            handle.close()  # type: ignore[union-attr]


@dataclass
class ServiceState:
    name: str
    state: str = "stopped"  # stopped | waiting | starting | running | crashed | failed
    detail: str = ""
    restarts: int = 0
    pid: int | None = None


class Service:
    """A long-running child process."""

    def __init__(self, name: str, command: Callable[[], list[str]], *, env: Callable[[], dict[str, str]],
                 log_dir: Path, cwd: Path | None = None, ready: Callable[[], bool] | None = None,
                 stop_file: Path | None = None, grace_s: float = 10.0, label: str | None = None) -> None:
        self.name = name
        self.label = label or name
        self._command = command
        self._env = env
        self.cwd = cwd
        self.log_path = log_dir / f"{name}.log"
        self._ready = ready
        self.stop_file = stop_file
        self.grace_s = grace_s
        self.proc: subprocess.Popen | None = None
        self.status = ServiceState(name)
        self.started_at = 0.0
        self.wanted = False  # should be running (False after stop())
        self._restart_at: float | None = None
        self._delay_i = 0
        self._log_handle = None
        self._lock = threading.Lock()

    # -- start / stop -----------------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            if self.proc is not None and self.proc.poll() is None:
                return
            if self.stop_file is not None:
                with contextlib.suppress(OSError):
                    self.stop_file.unlink()
            cmd = self._command()
            self._close_log()
            self._log_handle = open_log(self.log_path)
            self._log_handle.write(("$ " + " ".join(str(c) for c in cmd) + "\n").encode())
            log.info("Starting %s", self.name)
            self.proc = subprocess.Popen(cmd, env=self._env(), cwd=self.cwd, stdout=self._log_handle,
                                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **no_window_flags())
            self.started_at = time.monotonic()
            self.wanted = True
            self._restart_at = None
            self.status.state, self.status.detail, self.status.pid = "starting", "", self.proc.pid

    def is_ready(self) -> bool:
        if self.proc is None or self.proc.poll() is not None:
            return False
        try:
            return self._ready() if self._ready else True
        except Exception:  # noqa: BLE001 - "not ready yet"
            return False

    def stop(self, timeout: float | None = None) -> None:
        """Ask nicely (stop file), wait, then end it hard. Never raises."""
        with self._lock:
            self.wanted = False
            self._restart_at = None
            proc = self.proc
            if proc is None or proc.poll() is not None:
                self.status.state = "stopped"
                self._close_log()
                return
            grace = self.grace_s if timeout is None else timeout
            if self.stop_file is not None:
                with contextlib.suppress(OSError):
                    self.stop_file.parent.mkdir(parents=True, exist_ok=True)
                    self.stop_file.write_text("stop", encoding="utf-8")
            elif not IS_WINDOWS:
                with contextlib.suppress(OSError):
                    proc.terminate()  # SIGTERM: Node and PostgreSQL end cleanly
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                log.warning("%s did not stop within %.0f s: ending it", self.name, grace)
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
            self.status.state, self.status.pid = "stopped", None
            self._close_log()
            if self.stop_file is not None:
                with contextlib.suppress(OSError):
                    self.stop_file.unlink()

    def _close_log(self) -> None:
        if self._log_handle is not None:
            with contextlib.suppress(OSError):
                self._log_handle.close()
            self._log_handle = None

    # -- watching ---------------------------------------------------------------------------

    def check(self, now: float | None = None) -> ServiceState:
        """Called every second by the launcher: notice crashes, restart after a pause."""
        now = time.monotonic() if now is None else now
        st = self.status
        if not self.wanted or self.proc is None:
            return st
        code = self.proc.poll()
        if code is None:
            if st.state == "starting" and self.is_ready():
                st.state, st.detail = "running", ""
            if now - self.started_at > STABLE_AFTER_S:
                self._delay_i = 0
            return st
        if self._restart_at is None:
            delay = RESTART_DELAYS[min(self._delay_i, len(RESTART_DELAYS) - 1)]
            self._delay_i += 1
            self._restart_at = now + delay
            st.state = "crashed"
            st.detail = f"stopped unexpectedly (exit code {code}); starting again in {delay:.0f} s"
            st.pid = None
            log.warning("%s %s. See %s", self.name, st.detail, self.log_path)
        elif now >= self._restart_at:
            st.restarts += 1
            self.start()
        return st
