"""The desktop launcher without the real CountVision: configuration, background programs (start,
crash + restart, clean stop with the stop file, hard stop), one instance per computer, the
environment each part gets, and the window logic with a fake pywebview."""

from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from countvision_desktop import config, control, services, window
from countvision_desktop.config import Settings
from countvision_desktop.launcher import Launcher, Status, port_free
from countvision_desktop.splash import page, text_report


def wait_for(check, timeout: float = 10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if check():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    for name in ("CV_DESKTOP_HOME", "CV_INSTALL_DIR", "CV_PG_BIN_DIR", "CV_VIDEO_MODEL", "CV_DESKTOP_DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    model = tmp_path / "yolox_tiny.onnx"
    model.write_bytes(b"x")
    return config.load(home=tmp_path / "home", database_url="postgresql+psycopg://u:p@127.0.0.1:5433/db",
                       model=model)


# --- configuration ----------------------------------------------------------------------------------


def test_config_from_source_and_problems(cfg, tmp_path):
    assert cfg.install_dir == config.repo_root()
    assert cfg.data_dir == tmp_path / "home" / "data" and cfg.edge_yaml == cfg.data_dir / "edge" / "edge.yaml"
    assert not cfg.manages_postgres and cfg.app_url == "http://localhost:3000"
    bare = config.DesktopConfig(home=tmp_path, install_dir=tmp_path, data_dir=tmp_path / "d",
                                python=Path(sys.executable), node=None, web_dir=None, web_mode="next",
                                pg_bin=None, database_url=None, model=None)
    text = " ".join(bare.problems())
    assert "Node.js" in text and "npm run build" in text and "PostgreSQL" in text and "yolox_tiny" in text


def test_settings_keep_unknown_keys(tmp_path):
    path = tmp_path / "desktop.json"
    path.write_text(json.dumps({"allow_network": True, "window": {"width": 1000}, "api_env": {"A": "1"}}))
    s = Settings.load(path)
    assert s.allow_network and s.window == {"width": 1000} and s.extra == {"api_env": {"A": "1"}}
    s.allow_network = False
    s.save(path)
    assert json.loads(path.read_text()) == {"api_env": {"A": "1"}, "allow_network": False, "window": {"width": 1000}}
    path.write_text("not json")
    assert Settings.load(path).allow_network is False


def test_network_access_changes_the_web_host_and_links(cfg):
    assert cfg.web_host == "127.0.0.1" and cfg.public_url == "http://localhost:3000"
    cfg.settings.allow_network = True
    assert cfg.web_host == "0.0.0.0" and cfg.public_url.endswith(":3000") and "localhost" not in cfg.public_url


def test_each_part_gets_its_own_environment(cfg, monkeypatch):
    monkeypatch.setenv("CV_DATA_DIR", "/somewhere/else")  # left over in a cmd window from manual tests
    monkeypatch.setenv("CV_DEVICE_ID", "wrong")
    cfg.settings.extra["api_env"] = {"CV_VIDEO_DETECTOR": "blobs"}
    launcher = Launcher(cfg)
    api = launcher.api_env()
    assert api["CV_EDITION"] == "local" and api["CV_ENVIRONMENT"] == "production"
    assert api["CV_DATA_DIR"] == str(cfg.data_dir) and "CV_DEVICE_ID" not in api
    assert api["CV_DATABASE_URL"] == cfg.database_url and api["CV_VIDEO_MODEL"] == str(cfg.model)
    assert api["CV_LOCAL_LIVE_PORT"] == "8002" and api["CV_VIDEO_DETECTOR"] == "blobs"
    edge = launcher.edge_env()
    assert not [k for k in edge if k.startswith("CV_")]  # the agent reads CV_DATA_DIR etc. itself
    assert "PATH" in edge or "Path" in edge
    web = launcher.web_env()
    assert web["NEXT_TELEMETRY_DISABLED"] == "1" and web["HOSTNAME"] == "127.0.0.1"
    assert "--stop-file" in launcher._api_command() and "--stop-file" in launcher._edge_command()
    assert launcher._edge_command()[launcher._edge_command().index("--config") + 1] == str(cfg.edge_yaml)


# --- background programs ------------------------------------------------------------------------------


def child(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / f"child_{abs(hash(body))}.py"
    script.write_text(textwrap.dedent(body))
    return [sys.executable, str(script)]


def test_service_stops_cleanly_with_the_stop_file(tmp_path):
    stop = tmp_path / "run" / "stop-x"
    cmd = child(tmp_path, f"""
        import time, pathlib, sys
        print("hello", flush=True)
        while not pathlib.Path({str(stop)!r}).exists():
            time.sleep(0.05)
        print("clean exit", flush=True)
        sys.exit(0)
    """)
    svc = services.Service("x", lambda: cmd, env=lambda: dict(__import__("os").environ), log_dir=tmp_path / "logs",
                           stop_file=stop, grace_s=10)
    svc.start()
    assert wait_for(svc.is_ready) and svc.check().state == "running"
    started = time.time()
    svc.stop()
    assert time.time() - started < 5 and svc.status.state == "stopped" and not stop.exists()
    log = (tmp_path / "logs" / "x.log").read_text()
    assert "hello" in log and "clean exit" in log and "$ " in log


def test_service_that_ignores_the_stop_is_ended_hard(tmp_path):
    cmd = child(tmp_path, "import time\nwhile True: time.sleep(1)\n")
    svc = services.Service("stubborn", lambda: cmd, env=lambda: dict(__import__("os").environ),
                           log_dir=tmp_path, stop_file=tmp_path / "stop-s", grace_s=0.5)
    svc.start()
    assert wait_for(svc.is_ready)
    svc.stop()
    assert svc.proc.poll() is not None and svc.status.state == "stopped"


def test_crashed_service_is_started_again(tmp_path, monkeypatch):
    monkeypatch.setattr(services, "RESTART_DELAYS", (0.2, 0.4))
    counter = tmp_path / "runs"
    cmd = child(tmp_path, f"""
        import pathlib, time, sys
        p = pathlib.Path({str(counter)!r})
        n = int(p.read_text()) + 1 if p.exists() else 1
        p.write_text(str(n))
        if n == 1:
            sys.exit(3)  # the first run crashes
        time.sleep(30)
    """)
    svc = services.Service("flaky", lambda: cmd, env=lambda: dict(__import__("os").environ), log_dir=tmp_path)
    svc.start()
    assert wait_for(lambda: svc.check().state == "crashed")
    assert "exit code 3" in svc.status.detail
    assert wait_for(lambda: svc.check().state == "running")
    assert svc.status.restarts == 1 and counter.read_text() == "2"
    svc.stop(timeout=1)


def test_run_once_and_log_rotation(tmp_path, monkeypatch):
    log = tmp_path / "x.log"
    log.write_bytes(b"x" * 100)
    monkeypatch.setattr(services, "LOG_MAX_BYTES", 50)
    assert services.run_once([sys.executable, "-c", "print('done')"], log_path=log) == 0
    assert (tmp_path / "x.1.log").exists() and "done" in log.read_text()


def test_port_free(tmp_path):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        assert not port_free(s.getsockname()[1])
    assert port_free(0)


# --- one instance ----------------------------------------------------------------------------------


def test_second_start_shows_the_running_one(tmp_path):
    shown, quit_ = [], []
    server = control.ControlServer(tmp_path, {"show": lambda: shown.append(1) or {"shown": True},
                                              "quit": lambda: quit_.append(1) or {}})
    server.start()
    try:
        assert control.running_instance(tmp_path) is not None
        assert control.ask(tmp_path, "show") and shown == [1]
        assert control.ask(tmp_path, "quit") and quit_ == [1]
        lock = json.loads((tmp_path / control.LOCK_FILE).read_text())
        lock["token"] = "wrong"
        assert control._call(lock, "show") is None and shown == [1]
    finally:
        server.stop()
    assert not (tmp_path / control.LOCK_FILE).exists()
    assert control.ask(tmp_path, "show") is False


def test_stale_lock_is_ignored(tmp_path):
    (tmp_path / control.LOCK_FILE).write_text(json.dumps({"pid": 1, "port": 9, "token": "x"}))
    assert control.running_instance(tmp_path) is None


# --- start screen and launcher errors -----------------------------------------------------------


def test_start_screen_and_text_report():
    status = Status(steps=[], message="Port 3000 is used <by> another program").as_dict()
    status["steps"] = [{"key": "api", "label": "CountVision service", "state": "error", "detail": "it <crashed>"}]
    html = page(status)
    assert "window.cvStatus(" in html and "Try again" in html and "open_logs" in html
    assert "<crashed>" not in html.split("<script>")[0]  # details are escaped by the page script
    assert text_report(status) == "CountVision service: error (it <crashed>)\nPort 3000 is used <by> another program"


def test_missing_parts_end_in_a_clear_error(cfg, tmp_path):
    cfg.node = None
    launcher = Launcher(cfg)
    seen: list[str] = []
    launcher.on_change(lambda st: seen.append(st.phase))
    launcher.start()
    assert wait_for(lambda: launcher.status.phase == "error")
    assert "Node.js" in launcher.status.message
    launcher.stop()
    assert launcher.status.phase == "stopped" and "error" in seen


def test_busy_port_ends_in_a_clear_error(cfg):
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        cfg.web_dir = cfg.install_dir  # pretend the web app is built
        cfg.node = Path(sys.executable)
        cfg.web_port = s.getsockname()[1]
        launcher = Launcher(cfg)
        launcher.start()
        assert wait_for(lambda: launcher.status.phase == "error")
        message = launcher.status.message
        assert str(cfg.web_port) in message and "used by another program" in message
        assert "docker compose down" in message
        launcher.stop()


# --- the window, with a fake pywebview ----------------------------------------------------------


class FakeEvent:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, fn):
        self.handlers.append(fn)
        return self

    def fire(self, *args):
        return [h(*args) for h in self.handlers]


class FakeWindow:
    def __init__(self, **kw) -> None:
        self.kw = kw
        self.events = type("E", (), {})()
        for name in ("closing", "resized", "moved", "maximized", "restored"):
            setattr(self.events, name, FakeEvent())
        self.loaded: list[str] = []
        self.js: list[str] = []
        self.hidden = False
        self.destroyed = threading.Event()

    def load_url(self, url):
        self.loaded.append(url)

    def load_html(self, html):
        pass

    def evaluate_js(self, code):
        self.js.append(code)

    def hide(self):
        self.hidden = True

    def show(self):
        self.hidden = False

    def restore(self):
        pass

    def destroy(self):
        self.destroyed.set()


class FakeWebview:
    def __init__(self) -> None:
        self.window: FakeWindow | None = None
        self.started = threading.Event()

    def create_window(self, title, **kw):
        self.window = FakeWindow(title=title, **kw)
        return self.window

    def start(self, func, **kw):
        func()
        self.started.set()
        self.window.destroyed.wait(20)


class FakeLauncher:
    """Enough of a Launcher for the window."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.status = Status(steps=[], url=cfg.app_url)
        self.listeners = []
        self.stopped = threading.Event()
        self.stopped_event = self.stopped

    def on_change(self, fn):
        self.listeners.append(fn)
        fn(self.status)

    def set(self, phase):
        self.status.phase = phase
        for fn in self.listeners:
            fn(self.status)

    def stop(self):
        self.stopped.set()

    def retry(self):
        pass

    def set_allow_network(self, allow):
        pass


def test_window_shows_start_screen_then_app_and_hides_on_close(cfg, monkeypatch):
    fake = FakeWebview()
    launcher = FakeLauncher(cfg)
    app = window.WebviewApp(launcher, fake)
    tray_notes = []

    class FakeTray:
        def notify(self, text):
            tray_notes.append(text)

        def stop(self):
            pass

    monkeypatch.setattr(window.Tray, "create", classmethod(lambda cls, *a, **k: FakeTray()))
    runner = threading.Thread(target=app.run)
    runner.start()
    assert fake.started.wait(5)
    w = fake.window
    assert w.kw["title"] == "CountVision" and "Starting CountVision" in w.kw["html"]
    assert w.kw["min_size"] == window.MIN_SIZE and w.kw["background_color"] == "#0e1820"
    launcher.set("starting")
    assert any("cvStatus" in js for js in w.js)
    launcher.set("ready")
    assert w.loaded == ["http://localhost:3000"]
    # closing the window: hidden, counting goes on, told once
    assert w.events.closing.fire() == [False] and w.hidden and len(tray_notes) == 1
    w.events.closing.fire()
    assert len(tray_notes) == 1
    assert app.show() == {"shown": True} and not w.hidden
    w.events.resized.fire(1100, 700)
    w.events.moved.fire(-32000, -32000)  # minimized on Windows: not remembered
    w.events.moved.fire(40, 50)
    app.quit()
    runner.join(10)
    assert not runner.is_alive() and launcher.stopped.is_set()
    saved = json.loads(cfg.settings_path.read_text())
    assert saved["window"] == {"width": 1100, "height": 700, "x": 40, "y": 50}


def test_window_without_tray_quits_on_close(cfg, monkeypatch):
    fake = FakeWebview()
    launcher = FakeLauncher(cfg)
    app = window.WebviewApp(launcher, fake)
    monkeypatch.setattr(window.Tray, "create", classmethod(lambda cls, *a, **k: None))
    runner = threading.Thread(target=app.run)
    runner.start()
    assert fake.started.wait(5)
    assert fake.window.events.closing.fire() == [True]  # really closes
    fake.window.destroy()
    runner.join(10)
    assert launcher.stopped.is_set()


def test_headless_runs_until_quit(cfg):
    launcher = FakeLauncher(cfg)
    launcher.ready_event = threading.Event()
    launcher.ready_event.set()
    launcher.status.phase = "ready"
    runner = threading.Thread(target=window.run_headless, args=(launcher,))
    runner.start()
    assert wait_for(lambda: hasattr(launcher, "quit_app"))
    time.sleep(0.2)
    assert runner.is_alive()
    launcher.quit_app()
    runner.join(5)
    assert not runner.is_alive() and launcher.stopped.is_set()


def test_remember_keeps_the_choices_for_the_next_start(tmp_path, monkeypatch):
    monkeypatch.delenv("CV_DESKTOP_DATABASE_URL", raising=False)
    home = tmp_path / "home"
    first = config.load(home=home, data_dir=tmp_path / "old-data", database_url="postgresql+psycopg://a:b@h:1/db",
                        model=tmp_path / "m.onnx")
    first.remember()
    again = config.load(home=home)
    assert again.data_dir == tmp_path / "old-data" and again.database_url == "postgresql+psycopg://a:b@h:1/db"
    assert again.model == tmp_path / "m.onnx" and not again.manages_postgres
    other = config.load(home=home, data_dir=tmp_path / "new")  # an argument wins
    assert other.data_dir == tmp_path / "new"


def test_windows_icon_has_only_bitmap_frames():
    """System.Drawing.Icon (the window on Windows) cannot read PNG-compressed frames."""
    import struct

    data = (Path(window.__file__).parent / "assets" / "icon.ico").read_bytes()
    count = struct.unpack("<HHH", data[:6])[2]
    assert count >= 5
    for i in range(count):
        offset = struct.unpack("<I", data[6 + 16 * i + 12:6 + 16 * i + 16])[0]
        assert data[offset:offset + 4] != b"\x89PNG"
