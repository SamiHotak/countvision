"""The PostgreSQL that comes with CountVision Local (portable binaries, PostgreSQL licence).

* First start: ``initdb`` creates the data folder (UTF-8, user "countvision", a random password
  kept in ``home/db-password``, password login only).
* Start/stop with ``pg_ctl`` (on Windows it also drops administrator rights, which PostgreSQL
  needs: it refuses to run as an administrator).
* Listens on 127.0.0.1 only, port 5544, so a PostgreSQL the PC may already have stays untouched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import socket
import subprocess

from .config import PG_DB, PG_USER, DesktopConfig
from .services import no_window_flags, run_once

log = logging.getLogger(__name__)


class DatabaseError(RuntimeError):
    """PostgreSQL could not be prepared or started; the message says what to do."""


class PortablePostgres:
    def __init__(self, cfg: DesktopConfig) -> None:
        self.cfg = cfg
        self.data = cfg.db_dir
        self.port = cfg.pg_port
        self.log_path = cfg.logs_dir / "database.log"

    # -- password -----------------------------------------------------------------------------

    def password(self) -> str:
        path = self.cfg.pg_password_file
        with contextlib.suppress(OSError):
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(24)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
        return value

    @property
    def url(self) -> str:
        return f"postgresql+psycopg://{PG_USER}:{self.password()}@127.0.0.1:{self.port}/{PG_DB}"

    def _env(self) -> dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "CV_"))}
        env["PGPASSWORD"] = self.password()
        return env

    # -- steps ----------------------------------------------------------------------------------

    @property
    def initialized(self) -> bool:
        return (self.data / "PG_VERSION").is_file()

    def initialize(self) -> None:
        if self.initialized:
            return
        self.data.mkdir(parents=True, exist_ok=True)
        pwfile = self.cfg.run_dir / "pg-init-password"
        pwfile.parent.mkdir(parents=True, exist_ok=True)
        pwfile.write_text(self.password(), encoding="utf-8")
        try:
            log.info("Creating the database folder %s (first start)", self.data)
            code = run_once([str(self.cfg.pg_tool("initdb")), "-D", str(self.data), "-U", PG_USER,
                             f"--pwfile={pwfile}", "--auth=scram-sha-256", "-E", "UTF8", "--locale=C"],
                            env=self._env(), log_path=self.log_path, timeout=300)
        finally:
            with contextlib.suppress(OSError):
                pwfile.unlink()
        if code != 0 or not self.initialized:
            raise DatabaseError(f"Could not create the database folder (initdb exit code {code}). "
                                f"See {self.log_path}")

    def is_running(self) -> bool:
        code = run_once([str(self.cfg.pg_tool("pg_ctl")), "status", "-D", str(self.data)], env=self._env(),
                        timeout=30)
        return code == 0

    def accepts_connections(self) -> bool:
        with contextlib.suppress(OSError), socket.create_connection(("127.0.0.1", self.port), timeout=1):
            code = run_once([str(self.cfg.pg_tool("pg_isready")), "-h", "127.0.0.1", "-p", str(self.port),
                             "-U", PG_USER, "-d", "postgres"], env=self._env(), timeout=15)
            return code == 0
        return False

    def start(self) -> None:
        if self.is_running():
            log.info("PostgreSQL is already running")
            return
        log.info("Starting PostgreSQL on 127.0.0.1:%d", self.port)
        code = run_once([str(self.cfg.pg_tool("pg_ctl")), "start", "-D", str(self.data), "-w", "-t", "60",
                         "-l", str(self.cfg.logs_dir / "postgres-server.log"),
                         "-o", f"-p {self.port} -h 127.0.0.1 -c max_connections=60 -c unix_socket_directories=''"],
                        env=self._env(), log_path=self.log_path, timeout=120)
        if code != 0:
            raise DatabaseError(f"PostgreSQL did not start (pg_ctl exit code {code}). See "
                                f"{self.cfg.logs_dir / 'postgres-server.log'}. Is port {self.port} used by "
                                "another program?")

    def ensure_database(self) -> None:
        """Create the "countvision" database once."""
        psql = [str(self.cfg.pg_tool("psql")), "-h", "127.0.0.1", "-p", str(self.port), "-U", PG_USER,
                "-d", "postgres", "-tAc"]
        out = subprocess.run([*psql, f"SELECT 1 FROM pg_database WHERE datname = '{PG_DB}'"],
                             env=self._env(), capture_output=True, text=True, timeout=30, check=False,
                             **no_window_flags())
        if out.returncode != 0:
            raise DatabaseError(f"Could not log in to PostgreSQL: {out.stderr.strip()[:300]}")
        if out.stdout.strip() == "1":
            return
        code = run_once([*psql, f'CREATE DATABASE "{PG_DB}" ENCODING \'UTF8\''], env=self._env(),
                        log_path=self.log_path, timeout=60)
        if code != 0:
            raise DatabaseError(f"Could not create the database. See {self.log_path}")

    def stop(self) -> None:
        if not self.initialized:
            return
        run_once([str(self.cfg.pg_tool("pg_ctl")), "stop", "-D", str(self.data), "-m", "fast", "-w", "-t", "30"],
                 env=self._env(), log_path=self.log_path, timeout=60)
