"""Command line: countvision-cloud migrate | wait | cleanup | serve | sample-data | send-reports |
make-admin | stripe-setup.

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
    sd = sub.add_parser("sample-data", help="add a 'Demo café' site with 5 weeks of invented numbers")
    sd.add_argument("--org", required=True, help="organization id or slug")
    sd.add_argument("--days", type=int, default=35)
    sub.add_parser("send-reports", help="send the report emails that are due now")
    ma = sub.add_parser("make-admin", help="give a user the operator's admin page (all organizations)")
    ma.add_argument("--email", required=True)
    ma.add_argument("--remove", action="store_true", help="take the admin page away again")
    sub.add_parser("stripe-setup", help="create the CountVision products, prices and portal in Stripe")
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
    elif args.cmd == "sample-data":
        from sqlalchemy import or_, select

        from .db import db_session
        from .product.services import sample_data
        from .saas.models import Organization

        with db_session() as db:
            cond = Organization.slug == args.org
            try:
                import uuid

                cond = or_(cond, Organization.id == uuid.UUID(args.org))
            except ValueError:
                pass
            org = db.scalar(select(Organization).where(cond))
            if org is None:
                raise SystemExit(f"Organization not found: {args.org}")
            site = sample_data.create(db, org, days=args.days)
            print(f"Created '{site.name}' ({site.id}) in {org.name}")
    elif args.cmd == "send-reports":
        from .tasks import send_due_reports

        print(send_due_reports())
    elif args.cmd == "make-admin":
        from sqlalchemy import func, select

        from .db import db_session
        from .saas.models import User
        from .saas.services import audit

        with db_session() as db:
            user = db.scalar(select(User).where(func.lower(User.email) == args.email.strip().lower()))
            if user is None:
                raise SystemExit(f"No account with the email {args.email}. Sign up in the web app first.")
            user.is_platform_admin = not args.remove
            audit.record(db, "admin.removed" if args.remove else "admin.added", actor=None,
                         target_type="user", target_id=user.id, email=user.email)
            state = "OFF" if args.remove else "ON"
            print(f"{user.email}: admin page {state} (reload the page in the browser)")
    elif args.cmd == "stripe-setup":
        from .saas.services import billing

        settings = get_settings()
        if not settings.billing_enabled:
            raise SystemExit("CV_STRIPE_SECRET_KEY is not set. Put your sk_test_... key into cloud/.env.")
        print(f"Stripe {'TEST' if settings.stripe_test_mode else 'LIVE'} mode, {settings.stripe_api_url}")
        for line in billing.setup_stripe():
            print(" -", line)
        print("Done. Safe to run again after changing prices in saas/plans.py.")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("countvision_cloud.main:app", host=args.host, port=args.port, reload=args.reload,
                    proxy_headers=False)  # X-Forwarded-For is read in ratelimit.client_ip
    return 0


if __name__ == "__main__":
    sys.exit(main())
