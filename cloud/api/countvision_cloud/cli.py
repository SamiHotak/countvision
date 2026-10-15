"""Command line: countvision-cloud migrate | wait | cleanup | serve | sample-data | send-reports |
make-admin | stripe-setup | set-password | backup | restore | licence-keygen | licence-create | licence-show.

Docker runs "countvision-cloud wait && countvision-cloud migrate" before the API starts.
"""

from __future__ import annotations

import argparse
import contextlib
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


def _default_key_dir():
    """Where the licence PRIVATE key lives: in the user's home, never inside the repository."""
    from pathlib import Path

    return Path.home() / "CountVision-licence-signing"


def _set_password(email_addr: str, password: str | None) -> None:
    import getpass

    from sqlalchemy import func, select

    from .db import db_session
    from .saas import security
    from .saas.models import User
    from .saas.services import audit, auth

    if not password:
        password = getpass.getpass("New password (at least 10 characters): ")
        if getpass.getpass("Again: ") != password:
            raise SystemExit("The two passwords are different.")
    with db_session() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email_addr.strip().lower()))
        if user is None:
            raise SystemExit(f"No account with the email {email_addr}.")
        auth.check_password(password, user.email)
        user.password_hash = security.hash_password(password)
        auth.logout_everywhere(db, user, keep_token=None)
        audit.record(db, "user.password_set_cli", actor=None, target_type="user", target_id=user.id)
    print(f"New password set for {user.email}. Log in with it now.")


def _licence_keygen(args) -> None:
    from pathlib import Path

    from .local import licence, licence_key

    out = Path(args.out)
    target = out / "licence_private.pem"
    if target.exists():
        raise SystemExit(f"{target} exists already. One key pair is enough - keep using it. (Delete it only "
                         "if you really want new keys: every licence made so far stops working.)")
    out.mkdir(parents=True, exist_ok=True)
    key = licence.new_private_key()
    target.write_bytes(licence.private_key_pem(key))
    with contextlib.suppress(OSError):
        target.chmod(0o600)
    public = licence.public_text_of(key)
    print(f"PRIVATE key written to {target}")
    print("  -> NEVER commit it, never email it. Copy it to a USB stick as a backup.")
    print(f"PUBLIC key: {public}")
    module = Path(licence_key.__file__)
    if args.no_write_public:
        print(f"Put the public key into {module} (PUBLIC_KEY = \"...\") and commit that file.")
        return
    text = module.read_text(encoding="utf-8")
    new = text.replace('PUBLIC_KEY = ""', f'PUBLIC_KEY = "{public}"')
    if new == text:
        print(f"{module} already has a public key; NOT changed. Replace it by hand if you mean to.")
        return
    module.write_text(new, encoding="utf-8")
    print(f"Public key written into {module}. Commit that file (it is safe to publish).")


def _licence_create(args) -> None:
    from datetime import date
    from pathlib import Path

    from .local import licence

    key_file = Path(args.key)
    if not key_file.is_file():
        raise SystemExit(f"Private key not found: {key_file}. Run 'countvision-cloud licence-keygen' once.")
    private = licence.load_private_key(key_file.read_bytes())
    valid = date.fromisoformat(args.valid_until) if args.valid_until else None
    updates = date.fromisoformat(args.updates_until) if args.updates_until else None
    key = licence.create_key(private, customer=args.customer, cameras=args.cameras, valid_until=valid,
                             updates_until=updates)
    lic = licence.parse_key(key, licence.public_text_of(private))
    print(f"Licence {lic.id} for {lic.customer}: {lic.cameras} cameras, valid until "
          f"{lic.valid_until or 'forever'}, updates until {lic.updates_until or '-'}")
    print()
    print(key)
    if args.out:
        Path(args.out).write_text(key + "\n", encoding="utf-8")
        print(f"\nAlso written to {args.out}")


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
    sp = sub.add_parser("set-password", help="set a new password for an account (forgotten password, "
                        "no mail server)")
    sp.add_argument("--email", required=True)
    sp.add_argument("--password", help="the new password (asked for when left out)")
    bk = sub.add_parser("backup", help="local edition: write a backup file (database + secret key)")
    bk.add_argument("--folder", help="target folder (default: the backup folder)")
    rs = sub.add_parser("restore", help="local edition: replace ALL data with a backup file")
    rs.add_argument("file")
    rs.add_argument("--yes", action="store_true", help="do not ask")
    kg = sub.add_parser("licence-keygen", help="create the licence signing key pair (ONCE, on Ezat's laptop)")
    kg.add_argument("--out", default=str(_default_key_dir()), help="folder for the PRIVATE key")
    kg.add_argument("--no-write-public", action="store_true",
                    help="only print the public key, do not put it into local/licence_key.py")
    lc = sub.add_parser("licence-create", help="sign a licence key for a customer (needs the private key)")
    lc.add_argument("--key", default=str(_default_key_dir() / "licence_private.pem"))
    lc.add_argument("--customer", required=True)
    lc.add_argument("--cameras", type=int, required=True)
    lc.add_argument("--valid-until", help="YYYY-MM-DD; leave out for a licence that never ends")
    lc.add_argument("--updates-until", help="YYYY-MM-DD: end of the updates/support period (shown only)")
    lc.add_argument("--out", help="also write the key into this text file")
    ls = sub.add_parser("licence-show", help="check a licence key and show what it contains")
    ls.add_argument("key")
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
    elif args.cmd == "set-password":
        _set_password(args.email, args.password)
    elif args.cmd == "backup":
        from .db import db_session
        from .local import backup

        with db_session() as db:
            info = backup.create(db, custom=args.folder)
        print(f"Backup written: {info.path} ({info.size // 1024} kB)")
    elif args.cmd == "restore":
        from pathlib import Path

        from .db import db_session
        from .local import backup

        path = Path(args.file)
        meta = backup.check_file(path)
        if not args.yes:
            answer = input(f"Replace ALL data with the backup from {meta.get('created_at')} "
                           f"({meta.get('organization')})? Type yes: ")
            if answer.strip().lower() != "yes":
                raise SystemExit("Cancelled.")
        with db_session() as db:
            result = backup.restore(db, path)
        print(f"Restored {result['restored']}. Safety backup of the old data: {result['safety_backup']}")
    elif args.cmd == "licence-keygen":
        _licence_keygen(args)
    elif args.cmd == "licence-create":
        _licence_create(args)
    elif args.cmd == "licence-show":
        from .local import licence

        lic = licence.parse_key(args.key)
        for k, v in lic.as_dict().items():
            print(f"{k:14} {v}")
    elif args.cmd == "serve":
        import uvicorn

        uvicorn.run("countvision_cloud.main:app", host=args.host, port=args.port, reload=args.reload,
                    proxy_headers=False)  # X-Forwarded-For is read in ratelimit.client_ip
    return 0


if __name__ == "__main__":
    sys.exit(main())
