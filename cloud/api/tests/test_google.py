"""Google login with a fake Google (no network)."""

from __future__ import annotations

import base64
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from conftest import PASSWORD, signup
from fastapi.testclient import TestClient

from countvision_cloud.saas.services import google
from countvision_cloud.settings import get_settings


def id_token(**claims) -> str:
    def part(d: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    base = {"iss": "https://accounts.google.com", "aud": "client-123", "exp": int(time.time()) + 300,
            "sub": "google-sub-1", "email": "g@example.com", "email_verified": True, "name": "Gina"}
    base.update(claims)
    return f"{part({'alg': 'RS256'})}.{part(base)}.sig"


class FakeGoogle:
    def __init__(self, **claims) -> None:
        self.claims = claims
        self.calls: list[tuple[str, str, str]] = []

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict:
        self.calls.append((code, verifier, redirect_uri))
        return {"id_token": id_token(**self.claims)}


@pytest.fixture
def google_on(monkeypatch):
    monkeypatch.setenv("CV_GOOGLE_CLIENT_ID", "client-123")
    monkeypatch.setenv("CV_GOOGLE_CLIENT_SECRET", "secret-xyz")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def run_flow(app, fake: FakeGoogle, client: TestClient | None = None, next_path: str = "/app"):
    app.dependency_overrides[google.get_client] = lambda: fake
    client = client or TestClient(app, headers={"X-CountVision": "1"})
    start = client.get(f"/api/auth/google/start?next={next_path}", follow_redirects=False)
    assert start.status_code == 302
    q = parse_qs(urlparse(start.headers["location"]).query)
    assert q["code_challenge_method"] == ["S256"] and q["redirect_uri"] == ["http://testserver/api/auth/google/callback"]
    cb = client.get(f"/api/auth/google/callback?code=abc&state={q['state'][0]}", follow_redirects=False)
    return client, cb


def test_google_signup(google_on):
    from countvision_cloud.main import create_app

    app = create_app()
    fake = FakeGoogle()
    client, cb = run_flow(app, fake, next_path="/invite/xyz")
    assert cb.status_code == 302 and cb.headers["location"] == "http://testserver/invite/xyz"
    me = client.get("/api/me").json()
    assert me["email"] == "g@example.com" and me["email_verified"] and me["google_linked"]
    assert me["has_password"] is False
    assert fake.calls[0][0] == "abc" and len(fake.calls[0][1]) >= 43
    assert client.get("/api/auth/providers").json()["google"] is True


def test_google_bad_state_and_open_redirect(google_on):
    from countvision_cloud.main import create_app

    app = create_app()
    app.dependency_overrides[google.get_client] = lambda: FakeGoogle()
    client = TestClient(app, headers={"X-CountVision": "1"})
    client.get("/api/auth/google/start", follow_redirects=False)
    cb = client.get("/api/auth/google/callback?code=abc&state=forged", follow_redirects=False)
    assert cb.status_code == 302 and "/login?error=" in cb.headers["location"]
    assert client.get("/api/me").status_code == 401
    _, cb2 = run_flow(app, FakeGoogle(), next_path="//evil.example.com")
    assert cb2.headers["location"] == "http://testserver/app"


def test_google_unverified_email_refused(google_on):
    from countvision_cloud.main import create_app

    app = create_app()
    client, cb = run_flow(app, FakeGoogle(email_verified=False))
    assert "/login?error=" in cb.headers["location"]
    assert client.get("/api/me").status_code == 401


def test_google_wrong_audience_refused(google_on):
    from countvision_cloud.main import create_app

    app = create_app()
    _, cb = run_flow(app, FakeGoogle(aud="someone-else"))
    assert "/login?error=" in cb.headers["location"]


def test_google_links_unverified_account_and_drops_its_password(google_on):
    """Somebody registered g@example.com with a password but never confirmed it."""
    from countvision_cloud.main import create_app

    app = create_app()
    squatter = TestClient(app, headers={"X-CountVision": "1"})
    signup(squatter, "g@example.com", "Squatter")
    client, _ = run_flow(app, FakeGoogle())
    me = client.get("/api/me").json()
    assert me["google_linked"] and me["has_password"] is False and me["email_verified"]
    assert squatter.get("/api/me").status_code == 401  # squatter logged out
    login = squatter.post("/api/auth/login", json={"email": "g@example.com", "password": PASSWORD})
    assert login.status_code == 401


def test_google_disabled_by_default(client):
    assert client.get("/api/auth/google/start", follow_redirects=False).status_code == 404
