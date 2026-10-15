"""Tests of CountVision Local (CV_EDITION=local): a real PostgreSQL test database, but NO Redis.

CV_REDIS_URL points at a port where nothing listens: if any code still used Redis in the local
edition, these tests would fail. Run them separately from the cloud tests:

    pytest cloud/api/tests_local

The licence key pair is generated for the test session (the public key is passed with
CV_LICENCE_PUBLIC_KEY, which only development/test honour).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ADMIN_URL = os.environ.get(
    "CV_TEST_DATABASE_URL", "postgresql+psycopg://countvision:countvision@localhost:5433/countvision"
)
TEST_DB = "countvision_local_test"
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + f"/{TEST_DB}"
DATA_DIR = Path(tempfile.mkdtemp(prefix="cv-local-test-"))

PRIVATE_KEY = Ed25519PrivateKey.generate()
OTHER_KEY = Ed25519PrivateKey.generate()  # a forger's key: its licences must be refused

from countvision_cloud.local import licence as _licence  # noqa: E402  (no settings read at import)

os.environ.update({
    "CV_EDITION": "local",
    "CV_ENVIRONMENT": "test",
    "CV_DATABASE_URL": TEST_URL,
    "CV_REDIS_URL": "redis://127.0.0.1:1/0",  # nothing there: Redis must not be used
    "CV_EMAIL_BACKEND": "off",
    "CV_EMAIL_DELIVERY": "sync",
    "CV_LOCAL_BACKGROUND": "false",
    "CV_DATA_DIR": str(DATA_DIR),
    "CV_WEB_URL": "http://testserver",
    "CV_LOG_LEVEL": "WARNING",
    "CV_VIDEO_DETECTOR": "blobs",
    "CV_VIDEO_RUNNER": "thread",
    "CV_LICENCE_PUBLIC_KEY": _licence.public_text_of(PRIVATE_KEY),
    "CV_GOOGLE_CLIENT_ID": "would-be-ignored",
    "CV_GOOGLE_CLIENT_SECRET": "would-be-ignored",
    "CV_STRIPE_SECRET_KEY": "sk_test_would_be_ignored",
})

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from countvision_cloud.db import Base, get_engine, reset_engine  # noqa: E402
from countvision_cloud.product import models as _product_models  # noqa: E402,F401
from countvision_cloud.redis_client import get_redis, reset_clients  # noqa: E402
from countvision_cloud.saas.services import email  # noqa: E402
from countvision_cloud.settings import get_settings  # noqa: E402

PASSWORD = "correct-horse-battery"


@pytest.fixture(scope="session", autouse=True)
def database() -> Iterator[None]:
    admin = create_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    admin.dispose()
    get_settings.cache_clear()
    reset_clients()
    reset_engine()
    from countvision_cloud.cli import migrate

    migrate(TEST_URL)
    yield
    reset_engine()


@pytest.fixture(autouse=True)
def clean(database) -> Iterator[None]:
    """Empty tables, the in-process store, the edge folder and the outbox before each test."""
    reset_engine()  # a restore test replaces the schema: start every test with fresh connections
    tables = ", ".join(f'"{t.name}"' for t in reversed(Base.metadata.sorted_tables))
    with get_engine().begin() as conn:
        conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    get_redis().flushdb()
    email.SENT.clear()
    edge = get_settings().edge_dir
    for name in ("cloud.json", "edge.yaml"):
        (edge / name).unlink(missing_ok=True)
    yield


@pytest.fixture
def app():
    from countvision_cloud.main import create_app

    return create_app()


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app, headers={"X-CountVision": "1"})


@pytest.fixture
def client2(app) -> TestClient:
    return TestClient(app, headers={"X-CountVision": "1"})


def run_setup(client: TestClient, email_addr: str = "owner@example.com", **extra) -> dict:
    body = {"name": "Olga", "email": email_addr, "password": PASSWORD, "business": "Café Sonne",
            "site_name": "Main street", "timezone": "Europe/Berlin", **extra}
    resp = client.post("/api/local/setup", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def make_key(cameras: int = 4, *, key=None, **kw) -> str:
    return _licence.create_key(key or PRIVATE_KEY, customer="Café Sonne GmbH", cameras=cameras, **kw)
