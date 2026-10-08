"""Command line: countvision-cloud migrate | wait | cleanup | serve.

Docker runs "countvision-cloud wait && countvision-cloud migrate" before the API starts.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from importlib.resources import files

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from .logging_setup import setup_logging
from .settings import get_settings

log = logging.getLogger("countvision_cloud.cli")


def alembic_config(url: str | None = None) -> Config:
    """Alembic config without an ini file (works from any folder and inside Docker)."""
    cfg = Config()
    cfg.set_main_option("script_location", str(files("countvision_cloud") / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (url or get_settings().database_url).replace("%", "%%"))
    return cfg


def migrate(url: str | None = None, revision: str = "head") -> None:
    """Bring the database schema up to date (safe to run again)."""
    command.upgrade(alembic_config(url), revision)


def enable_timescale(url: str | None = None) -> bool:
    """Enable the TimescaleDB extension when the server has it (used from Phase 3 B on)."""
    from sqlalchemy import create_engine

    engine = create_engine(url or get_settings().database_url)
    try:
        with engine.begin() as conn:
            available = conn.scalar(text(
                "SELECT count(*) FROM pg_available_extensions WHERE name = 'timescaledb'"))
            if available:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS timescaledb"))
            return bool(available)
    finally:
        engine.dispose()


def wait(timeout_s: float = 60) -> None:
    """Wait until the database and Redis answer (containers start in any order)."""
    from .db import get_engine
    from .redis_client import get_redis

    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
            get_redis().ping()
            log.info("Database and Redis are ready")
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise SystemExit(f"Database/Redis not ready after {timeout_s:.0f} s: {exc}") from exc
            log.info("Waiting for database and Redis ... (%s)", type(exc).__name__)
            time.sleep(2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="countvision-cloud", description="CountVision cloud tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="update the database schema")
    w = sub.add_parser("wait", help="wait for database and Redis")
    w.add_argument("--timeout", type=float, default=60)
    sub.add_parser("cleanup", help="delete expired logins, tokens and invites now")
    s = sub.add_parser("serve", help="run the API (uvicorn)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8001)
    s.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    setup_logging(get_settings().log_level)

    if args.cmd == "migrate":
        has_ts = enable_timescale()
        migrate()
        log.info("Database is up to date (TimescaleDB %s)", "enabled" if has_ts else "not installed")
    elif args.cmd == "wait":
        wait(args.timeout)
    elif args.cmd == "cleanup":
        from .tasks import cleanup

        print(cleanup())
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("countvision_cloud.main:app", host=args.host, port=args.port, reload=args.reload,
                    proxy_headers=False)  # X-Forwarded-For is read in ratelimit.client_ip
    return 0


if __name__ == "__main__":
    sys.exit(main())
