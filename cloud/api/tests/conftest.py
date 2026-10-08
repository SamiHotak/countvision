"""Test setup: a real PostgreSQL test database (migrated with Alembic) and Redis database 15.

Local:  start Postgres + Redis (docker compose -f cloud/compose.yaml up -d db redis), then
        pytest cloud/api/tests
Change the servers with CV_TEST_DATABASE_URL / CV_TEST_REDIS_URL.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator

import pytest

ADMIN_URL = os.environ.get(
    "CV_TEST_DATABASE_URL", "postgresql+psycopg://countvision:countvision@localhost:5433/countvision"
)
TEST_DB = "countvision_test"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"  # same server, separate database

os.environ.update({
    "CV_ENVIRONMENT": "test",
    "CV_DATABASE_URL": TEST_URL,
    "CV_REDIS_URL": os.environ.get("CV_TEST_REDIS_URL", "redis://localhost:6379/15"),
    "CV_EMAIL_BACKEND": "memory",
    "CV_EMAIL_DELIVERY": "sync",
    "CV_WEB_URL": "http://testserver",
    "CV_GOOGLE_CLIENT_ID": "",
    "CV_GOOGLE_CLIENT_SECRET": "",
    "CV_LOG_LEVEL": "WARNING",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from countvision_cloud.db import Base, get_engine, reset_engine  # noqa: E402
from countvision_cloud.redis_client import get_redis  # noqa: E402
from countvision_cloud.saas.services import email  # noqa: E402
from countvision_cloud.settings import get_settings  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    """Create a fresh test database and run all migrations once."""
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()
    get_settings.cache_clear()
    reset_engine()
    from countvision_cloud.cli import enable_timescale, migrate

    enable_timescale(TEST_URL)
    migrate(TEST_URL)
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def clean(database) -> Iterator[None]:
    """Empty all tables, the Redis test database and the email outbox before each test."""
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    get_redis().flushdb()
    email.SENT.clear()
    yield


@pytest.fixture
def app():
    from countvision_cloud.main import create_app

    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    """A browser: keeps cookies, sends the CSRF header."""
    return TestClient(app, headers={"X-CountVision": "1"})


@pytest.fixture
def client2(app) -> TestClient:
    """A second browser (another person)."""
    return TestClient(app, headers={"X-CountVision": "1"})


PASSWORD = "correct-horse-battery"


def signup(client: TestClient, email_addr: str, name: str = "Test User", password: str = PASSWORD,
           invite_token: str | None = None) -> dict:
    body = {"email": email_addr, "name": name, "password": password}
    if invite_token:
        body["invite_token"] = invite_token
    resp = client.post("/api/auth/signup", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def create_org(client: TestClient, name: str = "Café Sonne") -> dict:
    resp = client.post("/api/orgs", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def last_link(to: str, path: str) -> str:
    """The token from the newest email to this address that has a link with this path."""
    for mail in reversed(email.SENT):
        if mail.to == to:
            m = re.search(rf"http://testserver/{re.escape(path)}[?/](?:token=)?([A-Za-z0-9_\-]+)", mail.text)
            if m:
                return m.group(1)
    raise AssertionError(f"no {path} email to {to}; sent: {[(m.to, m.subject) for m in email.SENT]}")
