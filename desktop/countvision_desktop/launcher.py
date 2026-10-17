"""Starts and watches every part of CountVision Local, in this order:

1. the database (the bundled PostgreSQL, or an existing one)
2. database update (``countvision-cloud migrate``: new versions bring new tables)
3. the CountVision service (the API, one process: it also runs the timers for alerts and reports)
4. the app (the Next.js web app the window shows)
5. the counting service (the edge agent), once the setup wizard has written its settings
   (``<data>/edge/edge.yaml``). It runs WITHOUT the API's CV_ settings, and is restarted when
   edge.yaml changes (a restore or an update writes a new one).

Crashed parts are started again after a short pause. ``stop()`` ends everything cleanly, the
database last. Progress is reported to listeners (the window's start screen).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import IS_WINDOWS, DesktopConfig
from .postgres import DatabaseError, PortablePostgres
from .services import Service, run_once

log = logging.getLogger(__name__)

READY_TIMEOUT_S = 90.0
CHILDREN_FILE = "children.json"
VERSION_KEY = "app_version"  # desktop.json: the version that last started (a different one = an update)
BACKUP_TIMEOUT_S = 1800.0

# Only these variables reach the counting service: it must not see the API's CV_ settings
# (it reads CV_DATA_DIR, CV_DEVICE_ID, CV_CLOUD_URL ... itself).
EDGE_ENV_KEEP = ("SYSTEMROOT", "SystemRoot", "WINDIR", "PATH", "Path", "TEMP", "TMP", "USERPROFILE", "HOME",
                 "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "ProgramData", "COMPUTERNAME", "USERNAME", "USER",
                 "LANG", "LC_ALL", "TZ", "HOMEDRIVE", "HOMEPATH", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE")


@dataclass
class Step:
    key: str
    label: str
    state: str = "waiting"  # waiting | working | done | error
    detail: str = ""


@dataclass
class Status:
    phase: str = "starting"  # starting | ready | error | stopping | stopped
    message: str = ""
    steps: list[Step] = field(default_factory=list)
    url: str = ""
    allow_network: bool = False
    public_url: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class StartError(RuntimeError):
    """Starting failed in a way the person can fix; the message says how."""


def port_free(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if not IS_WINDOWS:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
        return True


def http_ok(url: str, timeout: float = 2.0) -> bool:
    """Any HTTP answer below 500 (a 503 "degraded" health answer also means the server is up)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 500
    except urllib.error.HTTPError as exc:
        return exc.code in (503,) or exc.code < 500
    except (OSError, ValueError):
        return False


def file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


class Launcher:
    def __init__(self, cfg: DesktopConfig) -> None:
        self.cfg = cfg
        self.pg = PortablePostgres(cfg) if cfg.manages_postgres else None
        run = cfg.run_dir
        self.api = Service("api", self._api_command, env=self.api_env, log_dir=cfg.logs_dir,
                           cwd=cfg.data_dir, ready=lambda: http_ok(f"http://127.0.0.1:{cfg.api_port}/api/health"),
                           stop_file=run / "stop-api", grace_s=8.0, label="CountVision service")
        self.web = Service("web", self._web_command, env=self.web_env, log_dir=cfg.logs_dir, cwd=cfg.web_dir,
                           ready=lambda: http_ok(f"http://127.0.0.1:{cfg.web_port}/api/edition"),
                           grace_s=2.0, label="App")
        self.edge = Service("edge", self._edge_command, env=self.edge_env, log_dir=cfg.logs_dir,
                            cwd=cfg.edge_dir, stop_file=run / "stop-edge", grace_s=20.0, label="Counting service")
        self.status = Status(steps=[
            Step("database", "Database"),
            Step("migrate", "Database update"),
            Step("api", "CountVision service"),
            Step("web", "App"),
            Step("edge", "Counting service"),
        ], url=cfg.app_url, allow_network=cfg.settings.allow_network, public_url=cfg.public_url)
        self._listeners: list[Callable[[Status], None]] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._edge_hash: str | None = None
        self._lock = threading.RLock()
        self.ready_event = threading.Event()
        self.stopped_event = threading.Event()
        # set by the window (window.py): what a second start of CountVision asks for
        self.show_window: Callable[[], dict] = lambda: {"shown": False}
        self.quit_app: Callable[[], dict] = self._quit_without_window

    def _quit_without_window(self) -> dict:
        threading.Thread(target=self.stop, daemon=True).start()
        return {"quitting": True}

    # -- environment of the parts --------------------------------------------------------------

    def database_url(self) -> str:
        return self.pg.url if self.pg is not None else str(self.cfg.database_url)

    def api_env(self) -> dict[str, str]:
        cfg = self.cfg
        env = {k: v for k, v in os.environ.items() if not k.startswith("CV_")}
        env.update({
            "CV_EDITION": "local",
            "CV_ENVIRONMENT": "production",
            "CV_DATA_DIR": str(cfg.data_dir),
            "CV_DATABASE_URL": self.database_url(),
            "CV_WEB_URL": cfg.public_url,
            "CV_LOCAL_API_URL": f"http://127.0.0.1:{cfg.api_port}",
            "CV_LOCAL_LIVE_PORT": str(cfg.live_port),
            "CV_VIDEO_DETECTOR": "onnx",
            "CV_LOG_LEVEL": "INFO",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        })
        if cfg.model is not None:
            env["CV_VIDEO_MODEL"] = str(cfg.model)
        if cfg.pg_bin is not None:
            env["CV_PG_BIN_DIR"] = str(cfg.pg_bin)
        extra = cfg.settings.extra.get("api_env")  # desktop.json: overrides for tests and support
        if isinstance(extra, dict):
            env.update({str(k): str(v) for k, v in extra.items()})
        return env

    def web_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith("CV_")}
        env.update({"NODE_ENV": "production", "NEXT_TELEMETRY_DISABLED": "1", "PORT": str(self.cfg.web_port),
                    "HOSTNAME": self.cfg.web_host})
        return env

    def edge_env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if k in EDGE_ENV_KEEP}
        env.update({"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
        return env

    def _api_command(self) -> list[str]:
        return [str(self.cfg.python), "-m", "countvision_cloud.cli", "serve", "--host", "127.0.0.1",
                "--port", str(self.cfg.api_port), "--stop-file", str(self.api.stop_file)]

    def _web_command(self) -> list[str]:
        cfg = self.cfg
        assert cfg.node is not None and cfg.web_dir is not None
        if cfg.web_mode == "standalone":
            return [str(cfg.node), str(cfg.web_dir / "server.js")]
        return [str(cfg.node), str(cfg.web_dir / "node_modules" / "next" / "dist" / "bin" / "next"), "start",
                "--port", str(cfg.web_port), "--hostname", cfg.web_host]

    def _edge_command(self) -> list[str]:
        return [str(self.cfg.python), "-m", "countvision_edge", "run", "--config", str(self.cfg.edge_yaml),
                "--stop-file", str(self.edge.stop_file)]

    # -- status --------------------------------------------------------------------------------

    def on_change(self, listener: Callable[[Status], None]) -> None:
        self._listeners.append(listener)
        listener(self.status)

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener(self.status)
            except Exception:  # noqa: BLE001 - a broken window must not stop CountVision
                log.exception("Status listener failed")

    def _step(self, key: str, state: str, detail: str = "") -> None:
        for step in self.status.steps:
            if step.key == key:
                if (step.state, step.detail) == (state, detail):
                    return
                step.state, step.detail = state, detail
        self._notify()

    def _phase(self, phase: str, message: str = "") -> None:
        self.status.phase, self.status.message = phase, message
        self._notify()

    # -- start ---------------------------------------------------------------------------------

    def start(self) -> None:
        """Start everything in the background (returns at once)."""
        self._thread = threading.Thread(target=self._run, name="launcher", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            self._startup()
        except StartError as exc:
            log.error("CountVision could not start: %s", exc)
            self._phase("error", str(exc))
            return
        except Exception as exc:  # noqa: BLE001 - shown in the window
            log.exception("CountVision could not start")
            self._phase("error", f"Something went wrong while starting: {exc}. See {self.cfg.logs_dir}")
            return
        self._phase("ready")
        self.ready_event.set()
        self._monitor()

    def _startup(self) -> None:
        cfg = self.cfg
        try:
            cfg.make_dirs()
            probe = cfg.run_dir / "write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except PermissionError as exc:
            raise StartError(f"CountVision cannot write to its data folder {cfg.home}. It was set up for "
                             "another Windows user. Log in as that user, or ask the person who installed it "
                             "to run the setup again while you are logged in.") from exc
        problems = cfg.problems()
        if problems:
            raise StartError(" ".join(problems))
        self._end_leftovers()
        self._check_ports()

        self._step("database", "working")
        had_database = self.pg is not None and self.pg.initialized
        if self.pg is not None:
            try:
                self.pg.initialize()
                self.pg.start()
                if not self._wait(self.pg.accepts_connections, 60):
                    raise StartError(f"The database does not answer. See {cfg.logs_dir / 'postgres-server.log'}")
                self.pg.ensure_database()
            except DatabaseError as exc:
                self._step("database", "error", str(exc))
                raise StartError(str(exc)) from exc
        self._step("database", "done")

        if had_database and self.update_pending():
            self._backup_before_update()

        self._step("migrate", "working")
        code = run_once([str(cfg.python), "-m", "countvision_cloud.cli", "migrate"], env=self.api_env(),
                        cwd=cfg.data_dir, log_path=cfg.logs_dir / "api.log", timeout=600)
        if code != 0:
            self._step("migrate", "error")
            raise StartError(f"The database could not be updated (exit code {code}). See {cfg.logs_dir / 'api.log'}")
        self._step("migrate", "done")
        self._remember_version()

        for key, service in (("api", self.api), ("web", self.web)):
            if self._stop.is_set():
                return
            self._step(key, "working")
            service.start()
            self._remember_children()
            if not self._wait(service.is_ready, READY_TIMEOUT_S):
                self._step(key, "error")
                raise StartError(f"{service.label} did not start. See {service.log_path}")
            service.status.state = "running"
            self._step(key, "done")
        self._step("edge", "waiting", "starts after the first setup" if not cfg.edge_yaml.exists() else "")
        self._check_edge()

    # -- updates -------------------------------------------------------------------------------------

    def update_pending(self) -> bool:
        """A different version started last time (or none was noted): this start is an update."""
        return self.cfg.settings.extra.get(VERSION_KEY) != self.cfg.version

    def _backup_before_update(self) -> None:
        """Before a new version changes the database: a full backup (data/backups/before-update-...).
        A failed backup stops the start (Try again), so an update never touches data without one."""
        cfg = self.cfg
        before = cfg.settings.extra.get(VERSION_KEY) or "an earlier version"
        if not any(step.key == "backup" for step in self.status.steps):
            index = next(i for i, step in enumerate(self.status.steps) if step.key == "migrate")
            self.status.steps.insert(index, Step("backup", "Backup before the update"))
        self._step("backup", "working", f"{before} -> {cfg.version}")
        log.info("Update from %s to %s: backup first", before, cfg.version)
        code = run_once([str(cfg.python), "-m", "countvision_cloud.cli", "backup", "--prefix", "before-update",
                         "--kind", "before-update"], env=self.api_env(), cwd=cfg.data_dir,
                        log_path=cfg.logs_dir / "api.log", timeout=BACKUP_TIMEOUT_S)
        if code != 0:
            self._step("backup", "error")
            raise StartError(f"The backup before the update failed (exit code {code}), so the update was not "
                             f"applied to your data. Is the disk full? See {cfg.logs_dir / 'api.log'}")
        self._step("backup", "done")

    def _remember_version(self) -> None:
        settings = self.cfg.settings
        if settings.extra.get(VERSION_KEY) != self.cfg.version:
            settings.extra[VERSION_KEY] = self.cfg.version
            with contextlib.suppress(OSError):
                settings.save(self.cfg.settings_path)

    def _wait(self, check: Callable[[], bool], timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._stop.is_set():
            if check():
                return True
            self._stop.wait(0.5)
        return False

    def _check_ports(self) -> None:
        cfg = self.cfg
        wanted = [(cfg.api_port, "127.0.0.1"), (cfg.web_port, cfg.web_host), (cfg.live_port, "127.0.0.1")]
        if self.pg is not None and not self.pg.is_running():
            wanted.append((cfg.pg_port, "127.0.0.1"))
        busy = [port for port, host in wanted if not port_free(port, host)]
        if busy:
            ports = ", ".join(str(p) for p in busy)
            which = f"Port {ports} is" if len(busy) == 1 else f"Ports {ports} are"
            raise StartError(f"{which} used by another program, so CountVision cannot start. Close that "
                             "program first. (Is the CountVision cloud test setup still running in Docker? Stop it "
                             "with: docker compose down)")

    # -- leftovers of a launcher that did not end cleanly ----------------------------------------

    def _remember_children(self) -> None:
        children = {}
        try:
            import psutil
        except ImportError:  # pragma: no cover - psutil comes with the counting service
            return
        for svc in (self.api, self.web, self.edge):
            if svc.proc is not None and svc.proc.poll() is None:
                with contextlib.suppress(Exception):
                    children[svc.name] = {"pid": svc.proc.pid,
                                          "created": psutil.Process(svc.proc.pid).create_time()}
        with contextlib.suppress(OSError):
            (self.cfg.run_dir / CHILDREN_FILE).write_text(json.dumps(children), encoding="utf-8")

    def _end_leftovers(self) -> None:
        """Programs of an earlier run that are still there (the launcher was killed) are ended,
        identified by process id AND start time (a reused id is never touched)."""
        path = self.cfg.run_dir / CHILDREN_FILE
        try:
            children = json.loads(path.read_text(encoding="utf-8"))
            import psutil
        except (OSError, ValueError, ImportError):
            return
        for name, info in children.items():
            with contextlib.suppress(Exception):
                proc = psutil.Process(int(info["pid"]))
                if abs(proc.create_time() - float(info["created"])) < 1.0:
                    log.warning("Ending the %s of an earlier run (pid %s)", name, info["pid"])
                    proc.kill()
                    proc.wait(10)
        with contextlib.suppress(OSError):
            path.unlink()

    # -- watching ------------------------------------------------------------------------------

    def _check_edge(self) -> None:
        yaml = self.cfg.edge_yaml
        if not yaml.exists():
            return
        digest = file_hash(yaml)
        if not self.edge.wanted:
            self._edge_hash = digest
            self._step("edge", "working")
            self.edge.start()
            self._remember_children()
        elif digest != self._edge_hash:
            log.info("The counting service's settings changed: restarting it")
            self._edge_hash = digest
            self.edge.stop()
            self.edge.start()
            self._remember_children()
        state = self.edge.status
        if state.state == "starting" and self.edge.proc is not None and self.edge.proc.poll() is None \
                and time.monotonic() - self.edge.started_at > 3:
            state.state = "running"
        self._step("edge", {"running": "done", "starting": "working", "crashed": "error"}.get(state.state, "working"),
                   state.detail)

    def _monitor(self) -> None:
        next_db = time.monotonic() + 15
        while not self._stop.wait(1.0):
            with self._lock:
                if self._stop.is_set():
                    break
                for key, service in (("api", self.api), ("web", self.web)):
                    before = service.status.restarts
                    st = service.check()
                    self._step(key, "error" if st.state == "crashed" else "done" if st.state == "running"
                               else "working", st.detail)
                    if st.restarts != before:
                        self._remember_children()
                self.edge.check()
                self._check_edge()
                if self.pg is not None and time.monotonic() > next_db:
                    next_db = time.monotonic() + 15
                    if not self.pg.accepts_connections():
                        log.warning("The database stopped: starting it again")
                        self._step("database", "error", "stopped unexpectedly; starting it again")
                        try:
                            self.pg.start()
                            self._step("database", "done")
                        except DatabaseError as exc:
                            self._step("database", "error", str(exc))

    def retry(self) -> None:
        """After an error (port taken, web app not built ...): look again and start again."""
        if self.status.phase != "error":
            return
        with self._lock:
            for service in (self.edge, self.web, self.api):
                service.stop()
            self.cfg.refresh()
            self.web.cwd = self.cfg.web_dir
            self._stop = threading.Event()
            for step in self.status.steps:
                step.state, step.detail = "waiting", ""
        self._phase("starting")
        self.start()

    # -- network access ------------------------------------------------------------------------

    def set_allow_network(self, allow: bool) -> None:
        """Phones and other PCs in the shop network may open CountVision (restarts the app and the
        service: invitation links then use this PC's name)."""
        with self._lock:
            self.cfg.settings.allow_network = allow
            self.cfg.settings.save(self.cfg.settings_path)
            self.status.allow_network, self.status.public_url = allow, self.cfg.public_url
            if self.status.phase != "ready":
                return
            self.web.stop()
            self.api.stop()
            for service in (self.api, self.web):
                service.start()
                self._wait(service.is_ready, READY_TIMEOUT_S)
            self._remember_children()
        self._notify()

    # -- stop ----------------------------------------------------------------------------------

    def stop(self) -> None:
        """End everything cleanly (the counting service writes its last minute first)."""
        if self.stopped_event.is_set():
            return
        self._stop.set()
        self._phase("stopping")
        with self._lock:
            self.edge.stop()
            self.web.stop()
            self.api.stop()
            if self.pg is not None:
                with contextlib.suppress(Exception):
                    self.pg.stop()
        with contextlib.suppress(OSError):
            (self.cfg.run_dir / CHILDREN_FILE).unlink()
        self._phase("stopped")
        self.stopped_event.set()
        log.info("CountVision stopped")
