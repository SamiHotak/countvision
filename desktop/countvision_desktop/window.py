"""The CountVision window and the tray icon.

* Window: pywebview (BSD) on Microsoft Edge WebView2, the browser engine built into Windows 10/11:
  CountVision opens in its own window with its own icon, no address bar, no browser tabs. It shows
  the start screen first, then the app.
* Closing the window does NOT stop counting: CountVision keeps running in the background with an
  icon next to the clock (pystray). Its menu: Open CountVision, Allow phones and other PCs in this
  network, Open the log folder, Quit CountVision. Without the tray icon library, closing the
  window quits.
* Fallback without pywebview (e.g. Python 3.14 on the developer's laptop, where its .NET bridge may
  not install): Microsoft Edge in app mode (``msedge --app=...``), which is also a window without
  an address bar. Without Edge either, the address is printed and the default browser opens it.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from pathlib import Path

from .config import IS_WINDOWS, DesktopConfig
from .launcher import Launcher, Status
from .splash import page

log = logging.getLogger(__name__)

DEFAULT_SIZE = (1280, 820)
MIN_SIZE = (900, 600)


def open_folder(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        os.startfile(str(path))  # type: ignore[attr-defined]  # noqa: S606
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


class Tray:
    """The icon next to the clock. Optional: ``Tray.create`` returns None without pystray/Pillow."""

    def __init__(self, icon) -> None:
        self.icon = icon

    @classmethod
    def create(cls, launcher: Launcher, *, on_open: Callable[[], None], on_quit: Callable[[], None]) -> Tray | None:
        try:
            import pystray

            from .icon import tray_image
        except Exception as exc:  # noqa: BLE001 - ImportError, or no display on Linux
            log.info("No tray icon (%s)", exc)
            return None
        cfg = launcher.cfg

        def toggle_network(icon, item) -> None:
            allow = not cfg.settings.allow_network
            threading.Thread(target=launcher.set_allow_network, args=(allow,), daemon=True).start()
            if allow:
                icon.notify(f"Phones and other PCs in this network can open {cfg.public_url} in a browser.",
                            "CountVision")

        menu = pystray.Menu(
            pystray.MenuItem("Open CountVision", lambda icon, item: on_open(), default=True),
            pystray.MenuItem("Allow phones and other PCs in this network", toggle_network,
                             checked=lambda item: cfg.settings.allow_network),
            pystray.MenuItem("Open the log folder", lambda icon, item: open_folder(cfg.logs_dir)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit CountVision", lambda icon, item: on_quit()),
        )
        try:
            icon = pystray.Icon("CountVision", tray_image(64), "CountVision is counting", menu)
            icon.run_detached()
        except Exception as exc:  # noqa: BLE001
            log.info("No tray icon (%s)", exc)
            return None
        return cls(icon)

    def notify(self, text: str) -> None:
        with contextlib.suppress(Exception):
            self.icon.notify(text, "CountVision")

    def stop(self) -> None:
        with contextlib.suppress(Exception):
            self.icon.stop()


class WebviewApp:
    """The native window (pywebview)."""

    def __init__(self, launcher: Launcher, webview_module) -> None:
        self.launcher = launcher
        self.cfg: DesktopConfig = launcher.cfg
        self.webview = webview_module
        self.window = None
        self.tray: Tray | None = None
        self.quitting = False
        self.showing_app = False
        self._told_background = False
        self._lock = threading.Lock()

    # called from JavaScript on the start screen (window.pywebview.api.<name>())
    class Api:
        def __init__(self, app: WebviewApp) -> None:
            self._app = app

        def retry(self) -> None:
            threading.Thread(target=self._app.launcher.retry, daemon=True).start()

        def open_logs(self) -> None:
            open_folder(self._app.cfg.logs_dir)

        def quit(self) -> None:
            self._app.quit()

    def run(self) -> None:
        geo = self.cfg.settings.window
        width, height = int(geo.get("width", DEFAULT_SIZE[0])), int(geo.get("height", DEFAULT_SIZE[1]))
        self.window = self.webview.create_window(
            "CountVision", html=page(self.launcher.status.as_dict()), js_api=self.Api(self),
            width=max(width, MIN_SIZE[0]), height=max(height, MIN_SIZE[1]), x=geo.get("x"), y=geo.get("y"),
            min_size=MIN_SIZE, background_color="#0e1820", maximized=bool(geo.get("maximized")),
            text_select=True)
        events = self.window.events
        events.closing += self._on_closing
        events.resized += self._on_resized
        events.moved += self._on_moved
        with contextlib.suppress(AttributeError):
            events.maximized += lambda: self._remember(maximized=True)
            events.restored += lambda: self._remember(maximized=False)
        icon = Path(__file__).resolve().parent / "assets" / "icon.png"
        self.webview.start(self._started, private_mode=False, storage_path=str(self.cfg.home / "webview"),
                           icon=str(icon) if icon.is_file() else None)
        # the window is gone: make sure everything has stopped
        if self.tray is not None:
            self.tray.stop()
        self.launcher.stop()
        self.cfg.settings.save(self.cfg.settings_path)

    def _started(self) -> None:
        self.tray = Tray.create(self.launcher, on_open=self.show, on_quit=self.quit)
        self.launcher.on_change(self._status)

    def _status(self, status: Status) -> None:
        if self.window is None or self.quitting:
            return
        if status.phase == "ready" and not self.showing_app:
            self.showing_app = True
            self.window.load_url(self.cfg.app_url)
            return
        if not self.showing_app:
            import json

            with contextlib.suppress(Exception):
                self.window.evaluate_js(f"window.cvStatus && window.cvStatus({json.dumps(status.as_dict())})")

    # -- window events ---------------------------------------------------------------------------

    def _remember(self, **values) -> None:
        self.cfg.settings.window.update(values)

    def _on_resized(self, width: int, height: int) -> None:
        if not self.cfg.settings.window.get("maximized"):
            self._remember(width=width, height=height)

    def _on_moved(self, x: int, y: int) -> None:
        if x > -10000 and y > -10000:  # minimized windows report huge negative positions on Windows
            self._remember(x=x, y=y)

    def _on_closing(self):
        """The person closed the window: keep counting in the background (tray), or quit."""
        if self.quitting or self.tray is None:
            self.quitting = True
            return True
        self.window.hide()
        if not self._told_background:
            self._told_background = True
            self.tray.notify("CountVision keeps counting in the background. Open it again from this icon; "
                             "quit it there too.")
        self.cfg.settings.save(self.cfg.settings_path)
        return False  # cancel the close

    # -- actions ---------------------------------------------------------------------------------

    def show(self) -> dict:
        if self.window is not None:
            with contextlib.suppress(Exception):
                self.window.show()
                self.window.restore()
        return {"shown": True}

    def quit(self) -> dict:
        with self._lock:
            if self.quitting:
                return {"quitting": True}
            self.quitting = True

        def work() -> None:
            if self.window is not None:
                with contextlib.suppress(Exception):
                    self.window.load_html(page({**self.launcher.status.as_dict(), "phase": "stopping"}))
            self.launcher.stop()
            if self.tray is not None:
                self.tray.stop()
            if self.window is not None:
                with contextlib.suppress(Exception):
                    self.window.destroy()

        threading.Thread(target=work, name="quit", daemon=True).start()
        return {"quitting": True}


def find_edge() -> Path | None:
    if not IS_WINDOWS:
        return None
    for base in (os.environ.get("PROGRAMFILES(X86)"), os.environ.get("PROGRAMFILES"), os.environ.get("LOCALAPPDATA")):
        if base:
            candidate = Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"
            if candidate.is_file():
                return candidate
    found = shutil.which("msedge")
    return Path(found) if found else None


class EdgeAppWindow:
    """Fallback window: Microsoft Edge in app mode (own window, no address bar, own profile)."""

    def __init__(self, launcher: Launcher, edge: Path) -> None:
        self.launcher = launcher
        self.cfg = launcher.cfg
        self.edge = edge
        self.proc: subprocess.Popen | None = None
        self.quit_event = threading.Event()
        self.tray: Tray | None = None

    def open(self) -> dict:
        if self.proc is not None and self.proc.poll() is None:
            return {"shown": True}  # Edge brings its own window up when the same app is opened again
        geo = self.cfg.settings.window
        self.proc = subprocess.Popen([
            str(self.edge), f"--app={self.cfg.app_url}", f"--user-data-dir={self.cfg.home / 'browser'}",
            "--no-first-run", "--no-default-browser-check", "--disable-features=Translate",
            f"--window-size={geo.get('width', DEFAULT_SIZE[0])},{geo.get('height', DEFAULT_SIZE[1])}",
        ])
        return {"shown": True}

    def quit(self) -> dict:
        self.quit_event.set()
        return {"quitting": True}

    def run(self) -> None:
        self.tray = Tray.create(self.launcher, on_open=self.open, on_quit=self.quit)
        print("Starting CountVision ...")
        while not self.launcher.ready_event.wait(0.5):
            if self.launcher.status.phase == "error":
                print(self.launcher.status.message)
                self.launcher.stop()
                return
        self.open()
        while not self.quit_event.wait(1.0):
            if self.tray is None and self.proc is not None and self.proc.poll() is not None:
                break  # no tray icon: closing the window quits
        if self.tray is not None:
            self.tray.stop()
        self.launcher.stop()


def run_window(launcher: Launcher, *, prefer: str = "auto") -> None:
    """Show CountVision and block until the person quits. The window registers its "show" and
    "quit" actions on the launcher (``launcher.show_window`` / ``launcher.quit_app``), which a second
    start of CountVision calls through the control server."""
    webview = None
    if prefer in ("auto", "webview"):
        try:
            import webview  # type: ignore[import-not-found]
        except Exception as exc:  # noqa: BLE001 - ImportError, or pythonnet/.NET problems on Windows
            log.info("pywebview is not available (%s): using the fallback window", exc)
            webview = None
    if webview is not None:
        app = WebviewApp(launcher, webview)
        launcher.show_window = app.show
        launcher.quit_app = app.quit
        try:
            app.run()
            return
        except Exception as exc:  # noqa: BLE001 - e.g. the WebView2 runtime is missing
            log.exception("The CountVision window could not open (%s): using the fallback window", exc)
            if launcher.stopped_event.is_set():
                return
    edge = find_edge() if prefer in ("auto", "edge", "webview") else None
    if edge is not None:
        fallback = EdgeAppWindow(launcher, edge)
        launcher.show_window = fallback.open
        launcher.quit_app = fallback.quit
        fallback.run()
        return
    run_headless(launcher, open_browser=prefer != "none")


def run_headless(launcher: Launcher, *, open_browser: bool = False) -> None:
    """No window: print the progress and the address, run until Ctrl+C or "quit"."""
    from .splash import text_report

    quit_event = threading.Event()
    def quit_app() -> dict:
        quit_event.set()
        return {"quitting": True}

    launcher.quit_app = quit_app
    last = {"text": ""}

    def report(status: Status) -> None:
        text = text_report(status.as_dict())
        if text != last["text"] and status.phase in ("starting", "error"):
            last["text"] = text
            print(f"--- {status.phase}\n{text}", flush=True)

    launcher.on_change(report)
    try:
        while not launcher.ready_event.wait(0.5):
            if launcher.status.phase == "error":
                return
        print(f"\nCountVision is running: {launcher.cfg.app_url}  (Ctrl+C to stop)", flush=True)
        if open_browser:
            with contextlib.suppress(Exception):
                webbrowser.open(launcher.cfg.app_url)
        while not quit_event.wait(1.0):
            pass
    except KeyboardInterrupt:
        print("\nStopping CountVision ...", flush=True)
    finally:
        launcher.stop()
