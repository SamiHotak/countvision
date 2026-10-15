"""Backup and restore for CountVision Local (owner only).

A backup is ONE file, ``CountVision-backup-<date>_<time>.cvbackup`` (a zip archive):

* database.dump  the whole database (``pg_dump --format=custom``: compressed, all tables)
* secret_key     the installation's secret (camera passwords and webhook secrets are encrypted with
                 it; without it a restore on a new PC could not read them)
* meta.json      app version, database revision, time, business name

It goes to the backup folder (default <data_dir>/backups) or any folder the owner types, for
example a USB stick (E:\\). Treat a backup like the PC itself: it contains everything, including
the password hashes of the accounts.

Restore = replace ALL current data with the backup:
1. a safety backup of the current state is made first ("before-restore-...")
2. the database schema is emptied and the dump is loaded (pg_restore)
3. migrations bring an older backup up to this version (a backup from a NEWER version is refused)
4. the secret key from the backup is used from now on; the built-in edge agent gets a fresh token
Everybody is logged out afterwards (the login sessions come from the backup).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .. import __version__
from ..db import get_engine, reset_engine, utcnow
from ..saas.errors import AppError, NotFound
from ..settings import SECRET_FILE, get_settings

log = logging.getLogger(__name__)

SUFFIX = ".cvbackup"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.cvbackup$")
DUMP = "database.dump"
META = "meta.json"
SECRET = "secret_key"
MAX_UPLOAD_BYTES = 4 * 1024**3


class BackupError(AppError):
    def __init__(self, message: str, code: str = "backup_failed", status: int = 400) -> None:
        super().__init__(message, code=code, status=status)


@dataclass
class BackupInfo:
    name: str
    path: Path
    size: int
    created_at: str | None
    version: str | None
    revision: str | None
    organization: str | None
    kind: str  # manual | before-restore | upload

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "size": self.size, "created_at": self.created_at, "version": self.version,
                "revision": self.revision, "organization": self.organization, "kind": self.kind}


# --- PostgreSQL tools -------------------------------------------------------------------------------


def _tool(name: str) -> str:
    """Path of pg_dump / pg_restore: the bundled PostgreSQL (CV_PG_BIN_DIR) or the PATH."""
    s = get_settings()
    exe = name + (".exe" if os.name == "nt" else "")
    if s.pg_bin_dir:
        path = Path(s.pg_bin_dir) / exe
        if path.is_file():
            return str(path)
    found = shutil.which(name)
    if not found:
        raise BackupError(f"{name} was not found. Set CV_PG_BIN_DIR to the PostgreSQL 'bin' folder.",
                          code="pg_tools_missing", status=500)
    return found


def _conn_args() -> tuple[list[str], dict[str, str]]:
    url = make_url(get_settings().database_url)
    args = ["--host", url.host or "localhost", "--port", str(url.port or 5432),
            "--username", url.username or "postgres", "--dbname", url.database or "postgres"]
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    env.setdefault("PGCONNECT_TIMEOUT", "10")
    return args, env


def _run(cmd: list[str], env: dict[str, str], what: str) -> None:
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no black console window
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=3600,  # noqa: S603
                          creationflags=flags)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        log.error("%s failed (%s): %s", what, proc.returncode, proc.stderr)
        raise BackupError(f"{what} failed: {' '.join(detail)[:300]}", status=500)


def db_revision(db: Session) -> str | None:
    try:
        return db.scalar(text("SELECT version_num FROM alembic_version"))
    except Exception:  # noqa: BLE001
        return None


def known_revisions() -> list[str]:
    from alembic.script import ScriptDirectory

    from ..cli import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    return [rev.revision for rev in script.walk_revisions()]


# --- folders -----------------------------------------------------------------------------------------


def folder(custom: str | None = None) -> Path:
    """The backup folder (created when missing). A custom folder must exist already (a USB stick)."""
    if custom and custom.strip():
        path = Path(custom.strip()).expanduser()
        if not path.is_dir():
            raise BackupError(f"The folder {path} does not exist. Plug in the USB stick or check the path.",
                              code="folder_missing")
        return path
    path = get_settings().backups_path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _info(path: Path) -> BackupInfo:
    meta: dict[str, Any] = {}
    try:
        with zipfile.ZipFile(path) as z:
            meta = json.loads(z.read(META))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        pass
    kind = "before-restore" if path.name.startswith("before-restore") else meta.get("kind", "manual")
    return BackupInfo(name=path.name, path=path, size=path.stat().st_size, created_at=meta.get("created_at"),
                      version=meta.get("version"), revision=meta.get("revision"),
                      organization=meta.get("organization"), kind=kind)


def list_backups(custom: str | None = None) -> list[BackupInfo]:
    base = folder(custom)
    items = [_info(p) for p in base.glob(f"*{SUFFIX}") if p.is_file()]
    return sorted(items, key=lambda b: b.created_at or "", reverse=True)


def find(name: str, custom: str | None = None) -> Path:
    if not NAME_RE.match(name or ""):
        raise NotFound("Backup not found.")
    path = folder(custom) / name
    if not path.is_file():
        raise NotFound("Backup not found.")
    return path


def free_space(custom: str | None = None) -> int | None:
    try:
        return shutil.disk_usage(folder(custom)).free
    except OSError:
        return None


# --- create ------------------------------------------------------------------------------------------


def create(db: Session, *, custom: str | None = None, prefix: str = "CountVision-backup",
           kind: str = "manual") -> BackupInfo:
    """pg_dump into a new .cvbackup file. Returns its facts."""
    from ..saas.models import Organization

    target = folder(custom)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")  # local time: what the owner sees on the PC
    path = target / f"{prefix}-{stamp}{SUFFIX}"
    org = db.query(Organization).order_by(Organization.created_at).first()
    meta = {"app": "CountVision Local", "version": __version__, "revision": db_revision(db),
            "created_at": utcnow().isoformat(), "organization": org.name if org else None, "kind": kind,
            "format": 1}
    args, env = _conn_args()
    with tempfile.TemporaryDirectory(prefix="cv-backup-") as tmp:
        dump = Path(tmp) / DUMP
        _run([_tool("pg_dump"), *args, "--format=custom", "--no-owner", "--no-privileges",
              "--file", str(dump)], env, "Backup (pg_dump)")
        part = path.with_name(path.name + ".part")
        try:
            with zipfile.ZipFile(part, "w", compression=zipfile.ZIP_STORED) as z:  # the dump is compressed
                z.write(dump, DUMP)
                z.writestr(SECRET, get_settings().secret_key)
                z.writestr(META, json.dumps(meta, indent=2))
            os.replace(part, path)
        except OSError as exc:
            Path(part).unlink(missing_ok=True)
            raise BackupError(f"Could not write the backup to {target}: {exc}") from exc
    log.info("Backup written: %s (%d kB)", path, path.stat().st_size // 1024)
    return _info(path)


# --- restore -----------------------------------------------------------------------------------------


def check_file(path: Path) -> dict[str, Any]:
    """Read meta.json and refuse files that are not usable here."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            meta = json.loads(z.read(META))
    except (OSError, KeyError, ValueError, zipfile.BadZipFile):
        raise BackupError("This is not a CountVision backup file (or it is damaged).",
                          code="bad_backup") from None
    if DUMP not in names or SECRET not in names:
        raise BackupError("This backup file is incomplete.", code="bad_backup")
    revision = meta.get("revision")
    if revision and revision not in known_revisions():
        raise BackupError(f"This backup was made by a newer CountVision ({meta.get('version')}). "
                          "Install that version (or newer) first, then restore.", code="backup_too_new")
    return meta


def restore(db: Session, path: Path, *, actor_email: str | None = None) -> dict[str, Any]:
    """Replace all data with the backup. The caller's db session must not be used afterwards."""
    meta = check_file(path)
    safety = create(db, prefix="before-restore", kind="before-restore")
    db.rollback()
    db.close()
    args, env = _conn_args()
    with tempfile.TemporaryDirectory(prefix="cv-restore-") as tmp:
        with zipfile.ZipFile(path) as z:
            z.extract(DUMP, tmp)
            secret = z.read(SECRET).decode().strip()
        reset_engine()  # close our pooled connections
        engine = get_engine()
        with engine.begin() as conn:
            conn.execute(text("SET lock_timeout = '20s'"))
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        reset_engine()
        _run([_tool("pg_restore"), *args, "--no-owner", "--no-privileges", "--exit-on-error",
              "--single-transaction", str(Path(tmp) / DUMP)], env, "Restore (pg_restore)")
    from ..cli import migrate

    migrate()  # an older backup gets this version's tables
    _use_secret(secret)
    from ..db import db_session
    from ..redis_client import get_redis
    from . import setup

    with db_session() as fresh:
        setup.ensure_edge_files(fresh)
        device = setup.builtin_device(fresh)
        device_id = device.id if device else None
    if device_id is not None:
        from ..product import live

        live.bump_device(device_id)  # the edge agent gets the restored camera list at once
    with contextlib.suppress(Exception):
        get_redis().flushdb()  # rate limits and snapshot requests of the old state
    log.warning("Database restored from %s by %s (safety backup %s)", path.name, actor_email, safety.name)
    return {"restored": path.name, "backup_created_at": meta.get("created_at"),
            "safety_backup": safety.name, "version": meta.get("version")}


def _use_secret(secret: str) -> None:
    """From now on the backup's secret decrypts the restored camera passwords."""
    s = get_settings()
    if not secret or secret == s.secret_key:
        return
    path = Path(s.data_dir) / SECRET_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret + "\n", encoding="utf-8")
    s.secret_key = secret
    if os.environ.get("CV_SECRET_KEY"):
        log.warning("CV_SECRET_KEY is set in the environment: after a restart it overrides the restored "
                    "secret key. Remove it so %s is used.", path)


def upload_target(name: str, custom: str | None = None) -> Path:
    """Where an uploaded .cvbackup file goes (a new name if it exists already)."""
    base = Path(name or "").name
    if not base.endswith(SUFFIX):
        raise BackupError("Choose a .cvbackup file.", code="bad_backup")
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", base[: -len(SUFFIX)]).strip("-.") or "upload"
    target = folder(custom) / f"{stem[:80]}{SUFFIX}"
    if target.exists():
        target = target.with_name(f"{stem[:70]}-{datetime.now(UTC).strftime('%H%M%S')}{SUFFIX}")
    return target


def finish_upload(part: Path, target: Path) -> BackupInfo:
    """Check the uploaded file and give it its final name (or delete it)."""
    try:
        check_file(part)
    except BackupError:
        part.unlink(missing_ok=True)
        raise
    os.replace(part, target)
    return _info(target)
