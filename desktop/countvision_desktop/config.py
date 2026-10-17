"""Where everything is: the program files, the data, the ports, and what the person chose.

Two layouts are supported:

* installed (Phase 4.5 C, setup.exe): ``<install>\\python\\``, ``<install>\\node\\node.exe``,
  ``<install>\\pgsql\\bin\\``, ``<install>\\web\\server.js`` (Next.js standalone build),
  ``<install>\\models\\yolox_tiny.onnx``.
* from source (now): the git repository. The web app runs with ``next start`` from
  ``cloud/web`` (build it first with ``npm run build``), Node.js from the PATH, PostgreSQL either
  from ``--pg-bin`` (portable binaries) or an existing database (``--database-url``, e.g. the
  Docker database on port 5433).

Data (``home``), default ``C:\\ProgramData\\CountVision`` on Windows and
``~/.local/share/countvision-local`` elsewhere:

    home/data      CV_DATA_DIR of the API (secret key, backups, video jobs, edge/ = the agent)
    home/db        the PostgreSQL data folder (when the launcher runs PostgreSQL)
    home/logs      one log file per part (database, api, web, edge, launcher)
    home/run       lock file, stop files
    home/desktop.json   the person's choices (network access, window position)
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

IS_WINDOWS = os.name == "nt"
APP_NAME = "CountVision"
SETTINGS_FILE = "desktop.json"

API_PORT = 8001  # the web app's /api proxy is built for this port (API_URL at build time)
WEB_PORT = 3000
LIVE_PORT = 8002
PG_PORT = 5544  # not 5432: a PostgreSQL the PC may already have stays untouched
PG_USER = "countvision"
PG_DB = "countvision"

MODEL_NAME = "yolox_tiny.onnx"


def default_home() -> Path:
    if env := os.environ.get("CV_DESKTOP_HOME"):
        return Path(env)
    if IS_WINDOWS:
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / APP_NAME
    return Path.home() / ".local" / "share" / "countvision-local"


def repo_root() -> Path | None:
    """The git repository when running from source (desktop/countvision_desktop/config.py)."""
    root = Path(__file__).resolve().parents[2]
    return root if (root / "cloud" / "web").is_dir() and (root / "edge").is_dir() else None


def is_installed(install: Path) -> bool:
    """The setup.exe layout (``<install>\\web\\server.js`` next to ``<install>\\python``)."""
    return (install / "web" / "server.js").is_file() and (install / "python").is_dir()


def app_version(install: Path) -> str:
    """The installed version (``<install>\\version.txt``, written by the build), else the package's."""
    from . import __version__

    try:
        text = (install / "version.txt").read_text(encoding="utf-8").split()
    except OSError:
        text = []
    return text[0] if text else __version__


def default_install_dir() -> Path:
    if env := os.environ.get("CV_INSTALL_DIR"):
        return Path(env)
    exe_dir = Path(sys.executable).resolve().parent
    # installed: <install>\python\python.exe
    if (exe_dir.parent / "web" / "server.js").is_file():
        return exe_dir.parent
    return repo_root() or exe_dir


def child_python() -> Path:
    """The Python that runs the API and the counting service. pythonw.exe has no console at all,
    which breaks some libraries' output: children use python.exe (started without a window)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        return exe.parent / "python.exe"
    return exe


@dataclass
class Settings:
    """What the person chose (desktop.json). Unknown keys are kept."""

    allow_network: bool = False
    window: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> Settings:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        window = raw.get("window") if isinstance(raw.get("window"), dict) else {}
        extra = {k: v for k, v in raw.items() if k not in ("allow_network", "window")}
        return cls(allow_network=bool(raw.get("allow_network", False)), window=window, extra=extra)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({**self.extra, "allow_network": self.allow_network, "window": self.window},
                                  indent=2), encoding="utf-8")
        os.replace(tmp, path)


@dataclass
class DesktopConfig:
    """Everything the launcher needs (see ``load``)."""

    home: Path
    install_dir: Path
    data_dir: Path
    python: Path
    node: Path | None
    web_dir: Path | None  # folder with server.js (installed) or cloud/web (source)
    web_mode: str  # "standalone" | "next"
    pg_bin: Path | None
    database_url: str | None  # an existing database: the launcher does not run PostgreSQL then
    model: Path | None
    api_port: int = API_PORT
    web_port: int = WEB_PORT
    live_port: int = LIVE_PORT
    pg_port: int = PG_PORT
    settings: Settings = field(default_factory=Settings)

    # -- folders ------------------------------------------------------------------------------

    @property
    def db_dir(self) -> Path:
        return self.home / "db"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def run_dir(self) -> Path:
        return self.home / "run"

    @property
    def edge_dir(self) -> Path:
        return self.data_dir / "edge"

    @property
    def edge_yaml(self) -> Path:
        return self.edge_dir / "edge.yaml"

    @property
    def settings_path(self) -> Path:
        return self.home / SETTINGS_FILE

    @property
    def pg_password_file(self) -> Path:
        return self.home / "db-password"

    def make_dirs(self) -> None:
        for folder in (self.home, self.data_dir, self.logs_dir, self.run_dir):
            folder.mkdir(parents=True, exist_ok=True)

    # -- addresses ----------------------------------------------------------------------------

    @property
    def installed(self) -> bool:
        return is_installed(self.install_dir)

    @property
    def version(self) -> str:
        return app_version(self.install_dir)

    @property
    def manages_postgres(self) -> bool:
        return self.database_url is None

    @property
    def app_url(self) -> str:
        """What the window opens (always this computer)."""
        return f"http://localhost:{self.web_port}"

    @property
    def web_host(self) -> str:
        return "0.0.0.0" if self.settings.allow_network else "127.0.0.1"

    @property
    def public_url(self) -> str:
        """The address other devices use (invitation links): the PC name when network access is on."""
        if self.settings.allow_network:
            import socket

            return f"http://{socket.gethostname()}:{self.web_port}"
        return self.app_url

    def pg_tool(self, name: str) -> Path:
        assert self.pg_bin is not None
        return self.pg_bin / (name + (".exe" if IS_WINDOWS else ""))

    def remember(self) -> None:
        """Save the data folder, database, PostgreSQL folder and model in desktop.json, so the
        desktop icon starts CountVision the same way without arguments."""
        extra = self.settings.extra
        extra["remember_install"] = str(self.install_dir)
        extra["data_dir"] = str(self.data_dir)
        for key, value in (("database_url", self.database_url), ("pg_bin", self.pg_bin), ("model", self.model)):
            if value:
                extra[key] = str(value)
            else:
                extra.pop(key, None)
        self.settings.save(self.settings_path)

    def refresh(self) -> None:
        """Look again for what was missing (after the person installed or built it)."""
        if self.node is None:
            self.node = _find_node(self.install_dir)
        if self.web_dir is None:
            self.web_dir, self.web_mode = _find_web(self.install_dir)
        if self.pg_bin is None and self.database_url is None:
            self.pg_bin = _find_pg_bin(self.install_dir)
        if self.model is None:
            self.model = _find_model(self.install_dir, self.home)

    def problems(self) -> list[str]:
        """Things that are missing, in words a person can act on. Empty = ready to start."""
        out = []
        if self.node is None:
            out.append("Node.js was not found. Install it (winget install OpenJS.NodeJS.LTS) or reinstall CountVision.")
        if self.web_dir is None:
            out.append("The web app is not built. In cloud\\web run: npm install, then npm run build.")
        if self.database_url is None and self.pg_bin is None:
            out.append("PostgreSQL was not found. Give the folder with pg_ctl (--pg-bin) or an existing database "
                       "(--database-url).")
        if self.model is None:
            out.append(f"The detector model {MODEL_NAME} was not found. Download it once with: "
                       f"countvision-edge models download --dir {self.home / 'models'}")
        return out


def _find_node(install: Path) -> Path | None:
    for candidate in (install / "node" / ("node.exe" if IS_WINDOWS else "node"),):
        if candidate.is_file():
            return candidate
    found = shutil.which("node")
    return Path(found) if found else None


def _find_web(install: Path) -> tuple[Path | None, str]:
    if (install / "web" / "server.js").is_file():
        return install / "web", "standalone"
    web = install / "cloud" / "web"
    if (web / ".next" / "BUILD_ID").is_file() and (web / "node_modules" / "next").is_dir():
        return web, "next"
    return None, "next"


def _find_pg_bin(install: Path) -> Path | None:
    if env := os.environ.get("CV_PG_BIN_DIR"):
        return Path(env)
    exe = "pg_ctl.exe" if IS_WINDOWS else "pg_ctl"
    for candidate in (install / "pgsql" / "bin",):
        if (candidate / exe).is_file():
            return candidate
    found = shutil.which("pg_ctl")
    return Path(found).parent if found else None


def _find_model(install: Path, home: Path) -> Path | None:
    if env := os.environ.get("CV_VIDEO_MODEL"):
        return Path(env)
    for candidate in (install / "models" / MODEL_NAME, home / "models" / MODEL_NAME,
                      install / "eval" / "models" / MODEL_NAME, install / "edge" / "models" / MODEL_NAME):
        if candidate.is_file():
            return candidate
    return None


def remembered(saved: dict[str, Any], install: Path) -> dict[str, Any]:
    """What ``--remember`` saved, if it belongs to this program folder. The choices saved while
    testing from the source code (e.g. the Docker database) must not leak into the installed app,
    which uses its own PostgreSQL and data folder."""
    owner = saved.get("remember_install")
    if owner == str(install) or (owner is None and not is_installed(install)):
        return saved
    return {}


def load(*, home: Path | None = None, install_dir: Path | None = None, data_dir: Path | None = None,
         database_url: str | None = None, pg_bin: Path | None = None, model: Path | None = None) -> DesktopConfig:
    """Find everything. Arguments win over environment variables, which win over what was saved
    with ``--remember`` (desktop.json), which wins over the defaults."""
    home = Path(home) if home else default_home()
    install = Path(install_dir) if install_dir else default_install_dir()
    saved = remembered(Settings.load(home / SETTINGS_FILE).extra, install)
    data_dir = data_dir or (Path(saved["data_dir"]) if saved.get("data_dir") else None)
    pg_bin = pg_bin or (Path(saved["pg_bin"]) if saved.get("pg_bin") else None)
    model = model or (Path(saved["model"]) if saved.get("model") else None)
    web_dir, web_mode = _find_web(install)
    database_url = database_url or os.environ.get("CV_DESKTOP_DATABASE_URL") or saved.get("database_url") or None
    cfg = DesktopConfig(
        home=home,
        install_dir=install,
        data_dir=Path(data_dir) if data_dir else home / "data",
        python=child_python(),
        node=_find_node(install),
        web_dir=web_dir,
        web_mode=web_mode,
        pg_bin=Path(pg_bin) if pg_bin else _find_pg_bin(install),
        database_url=database_url,
        model=Path(model) if model else _find_model(install, home),
    )
    cfg.settings = Settings.load(cfg.settings_path)
    return cfg
