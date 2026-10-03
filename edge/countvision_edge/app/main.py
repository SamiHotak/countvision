"""Start the local web app: build the detector and the runner, then serve the page."""

from __future__ import annotations

import errno
import logging
import socket
import threading
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..config import SourceConfig, load_config
from ..detectors import build_detector
from ..errors import CountVisionError
from ..logging_setup import setup_logging
from ..storage import SqliteBuffer
from ..testing.synthetic import demo_scene
from .runner import LiveRunner, PreviewMode
from .server import LOOPBACK_HOSTS, create_app, make_access_key

log = logging.getLogger(__name__)


@dataclass
class AppOptions:
    config: str | None
    camera: str | None = None
    source: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    open_browser: bool = True
    preview: PreviewMode = "full"
    demo: bool = False
    demo_dir: str = "demo_app"
    log_level: str | None = None


def prepare_demo(folder: str | Path) -> Path:
    """Write the synthetic demo video and a config for it. Returns the config path.

    The demo needs no camera and no model: coloured shapes walk across a counting line and
    wait in a queue zone. The video plays at normal speed, so it looks like a live camera.
    An existing demo config is kept, so lines you drew in the demo stay.
    """
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    video = out / "demo_scene.avi"
    if not video.is_file():
        demo_scene().write_video(video)
    config = out / "demo.yaml"
    if not config.is_file():
        data = {
            "data_dir": str(out / "data"),
            "log_level": "INFO",
            "heartbeat_interval_s": 60,
            "detector": {"type": "blobs"},
            "cameras": [{
                "id": "demo",
                "name": "Demo scene",
                "source": {"uri": str(video), "realtime": True},
                "classes": ["person", "car"],
                "coordinates": "normalized",
                "lines": [{"name": "entrance", "p1": [0.1, 0.5], "p2": [0.9, 0.5],
                           "in_direction": "to_right"}],
                "zones": [{"name": "waiting", "kind": "queue",
                           "polygon": [[0.05, 0.62], [0.45, 0.62], [0.45, 0.98], [0.05, 0.98]]}],
            }],
        }
        header = "# CountVision demo config (made by 'countvision-edge app --demo').\n"
        config.write_text(header + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config


def _check_port(host: str, port: int) -> None:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)) or "in use" in str(exc):
                raise CountVisionError(
                    f"Port {port} is already used. Is CountVision already open? "
                    f"Close it, or start with --port {port + 1}"
                ) from exc
            raise CountVisionError(f"Cannot listen on {host}:{port}: {exc}") from exc


def run_app(options: AppOptions) -> int:
    """Run until Ctrl+C. Returns the exit code."""
    import uvicorn

    config_path = prepare_demo(options.demo_dir) if options.demo else options.config
    if config_path is None:
        raise CountVisionError("Give a config file (--config) or use --demo.")
    cfg = load_config(config_path)
    setup_logging(options.log_level or cfg.log_level)
    cam = cfg.camera(options.camera)
    if options.source:
        cam = cam.model_copy(update={"source": SourceConfig(uri=options.source)})

    loopback = options.host in LOOPBACK_HOSTS
    _check_port(options.host, options.port)
    detector = build_detector(cfg.detector)
    buffer = SqliteBuffer(
        cfg.db_file(),
        retention_days=cfg.storage.retention_days,
        heartbeat_retention_hours=cfg.storage.heartbeat_retention_hours,
    )
    runner = LiveRunner(cfg, cam, detector, buffer, config_path=config_path, preview=options.preview)
    access_key = None if loopback else make_access_key()
    stopping = threading.Event()
    app = create_app(
        runner,
        stopping=stopping,
        access_key=access_key,
        trusted_hosts={"127.0.0.1", "localhost", "[::1]"} if loopback else None,
    )

    shown_host = "127.0.0.1" if options.host in ("0.0.0.0", "::") or loopback else options.host
    url = f"http://{shown_host}:{options.port}/"
    if access_key:
        url += f"?key={access_key}"
    print(f"\nCountVision is running: {url}")
    if not loopback:
        lan = _lan_ip()
        if lan:
            print(f"On your phone (same Wi-Fi): http://{lan}:{options.port}/?key={access_key}")
        print("The page is open to your network. Only people with the key can use it.")
        if options.preview == "full":
            print("Tip: --preview blur hides people in the preview picture.")
    print(f"Detector licence: {detector.info.license}")
    print("Press Ctrl+C to stop.\n")

    class Server(uvicorn.Server):
        def handle_exit(self, sig, frame) -> None:  # Ctrl+C: end the endless streams first
            stopping.set()
            super().handle_exit(sig, frame)

    server = Server(uvicorn.Config(
        app, host=options.host, port=options.port, log_level="warning",
        timeout_graceful_shutdown=3, access_log=False,
    ))
    if options.open_browser:
        def open_when_ready() -> None:
            for _ in range(100):
                if server.started:
                    webbrowser.open(url)
                    return
                time.sleep(0.1)
        threading.Thread(target=open_when_ready, daemon=True).start()
    try:
        server.run()
    except KeyboardInterrupt:
        pass  # second Ctrl+C: stop without waiting
    finally:
        runner.stop()
        buffer.close()
        detector.close()
    print("Stopped. The numbers are saved in", cfg.db_file())
    return 0


def _lan_ip() -> str | None:
    """This computer's address in the local network (no packet is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            return sock.getsockname()[0]
    except OSError:
        return None
