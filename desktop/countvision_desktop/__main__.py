"""Start CountVision Local.

    countvision-desktop                     the window (start screen, then the app)
    countvision-desktop --no-window         everything in the background, address printed (Ctrl+C stops)
    countvision-desktop --check             show what was found and what is missing, start nothing
    countvision-desktop --quit              stop a running CountVision cleanly

From source on Windows (with the Docker database of the earlier tests):

    .venv\\Scripts\\countvision-desktop --data-dir C:\\CountVisionLocal ^
        --database-url postgresql+psycopg://countvision:countvision@127.0.0.1:5433/countvision_local
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from pathlib import Path

from . import __version__
from .config import DesktopConfig, load


def setup_logging(cfg: DesktopConfig, console: bool) -> None:
    cfg.logs_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [logging.handlers.RotatingFileHandler(
        cfg.logs_dir / "launcher.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers, force=True)


def describe(cfg: DesktopConfig) -> str:
    def show(value) -> str:
        return str(value) if value else "NOT FOUND"

    lines = [
        f"CountVision desktop {__version__}",
        f"  data:        {cfg.home}  (CountVision data in {cfg.data_dir})",
        f"  program:     {cfg.install_dir}",
        f"  Python:      {cfg.python}",
        f"  Node.js:     {show(cfg.node)}",
        f"  web app:     {show(cfg.web_dir)} ({cfg.web_mode})",
        "  database:    " + (f"existing database {cfg.database_url.split('@')[-1]}" if cfg.database_url
                            else f"PostgreSQL from {show(cfg.pg_bin)}, port {cfg.pg_port}"),
        f"  model:       {show(cfg.model)}",
        f"  ports:       app {cfg.web_port}, service {cfg.api_port}, live picture {cfg.live_port}",
        f"  network:     {'phones and other PCs may connect' if cfg.settings.allow_network else 'this computer only'}",
    ]
    problems = cfg.problems()
    lines.append("Ready to start." if not problems else "Missing:\n  - " + "\n  - ".join(problems))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="countvision-desktop", description="Start CountVision Local")
    parser.add_argument("--home", type=Path, help="folder for data, database and logs "
                        "(default C:\\ProgramData\\CountVision)")
    parser.add_argument("--data-dir", type=Path, help="CountVision data folder (default <home>\\data)")
    parser.add_argument("--database-url", help="use an existing PostgreSQL database instead of the bundled one")
    parser.add_argument("--pg-bin", type=Path, help="folder with pg_ctl, initdb, pg_dump (portable PostgreSQL 16)")
    parser.add_argument("--model", type=Path, help="the detector model (yolox_tiny.onnx)")
    parser.add_argument("--install-dir", type=Path, help="program folder (default: found automatically)")
    parser.add_argument("--no-window", action="store_true", help="no window: run in the background")
    parser.add_argument("--window", choices=["auto", "webview", "edge"], default="auto",
                        help="which window to use (default: the native window, else Edge app mode)")
    parser.add_argument("--check", action="store_true", help="show what was found, start nothing")
    parser.add_argument("--remember", action="store_true",
                        help="save --data-dir, --database-url, --pg-bin and --model for the next starts "
                        "(then the countvision-app icon needs no arguments)")
    parser.add_argument("--quit", action="store_true", help="stop a running CountVision")
    args = parser.parse_args(argv)

    cfg = load(home=args.home, install_dir=args.install_dir, data_dir=args.data_dir,
               database_url=args.database_url, pg_bin=args.pg_bin, model=args.model)
    if args.remember:
        cfg.remember()
        print(f"Saved in {cfg.settings_path}")
    if args.check:
        print(describe(cfg))
        return 0 if not cfg.problems() else 1

    from .control import ControlServer, ask

    if args.quit:
        if not ask(cfg.run_dir, "quit"):
            print("CountVision is not running.")
            return 1
        import time

        from .control import running_instance

        deadline = time.monotonic() + 90  # the counting service saves its last minute first
        while running_instance(cfg.run_dir) is not None and time.monotonic() < deadline:
            time.sleep(0.5)
        if running_instance(cfg.run_dir) is not None:
            print("CountVision is still stopping. See the log folder:", cfg.logs_dir)
            return 1
        print("CountVision stopped.")
        return 0
    if ask(cfg.run_dir, "show"):  # already running: bring its window to the front
        print("CountVision is already running.")
        return 0

    setup_logging(cfg, console=args.no_window)
    logging.getLogger(__name__).info("CountVision desktop %s starting\n%s", __version__, describe(cfg))

    from .launcher import Launcher
    from .window import run_headless, run_window

    launcher = Launcher(cfg)
    control = ControlServer(cfg.run_dir, {"show": lambda: launcher.show_window(),
                                          "quit": lambda: launcher.quit_app(),
                                          "status": lambda: launcher.status.as_dict()})
    control.start()
    launcher.start()
    try:
        if args.no_window:
            run_headless(launcher)
        else:
            run_window(launcher, prefer=args.window)
    finally:
        launcher.stop()
        control.stop()
    return 0 if launcher.status.phase in ("stopped", "ready") else 1


if __name__ == "__main__":
    sys.exit(main())
