"""Login with Google (OpenID Connect, authorization code flow with PKCE).

Flow: /api/auth/google/start -> Google -> /api/auth/google/callback?code&state.
The state + PKCE verifier live in a short signed cookie. We exchange the code directly with
Google's token endpoint over TLS; per OpenID Connect Core 3.1.3.7 the ID token from that direct
call may be trusted without a separate signature check. We still check issuer, audience,
expiry and that Google verified the email.

Account linking rule (prevents a takeover by someone who registered the address earlier
with a password but never confirmed it): when we link Google to an UNCONFIRMED password
account, its password is removed and all its sessions are logged out.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlencode

import httpx
from sqlalchemy.orm import Session

from ...db import utcnow
from ...settings import Settings, get_settings
from .. import security
from ..errors import AppError
from ..models import User
from ..repositories import users as repo
from . import audit

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
STATE_COOKIE = "cv_google_state"
STATE_MAX_AGE_S = 600


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    name: str


class GoogleClient(Protocol):
    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict: ...


class HttpGoogleClient:
    """Talks to Google's token endpoint."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def exchange(self, code: str, verifier: str, redirect_uri: str) -> dict:
        resp = httpx.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=15,
        )
        if resp.status_code != 200:
            log.warning("Google token exchange failed: %s %s", resp.status_code, resp.text[:300])
            raise AppError("Google login failed. Please try again.", code="google_failed")
        return resp.json()


def redirect_uri(settings: Settings) -> str:
    return f"{settings.web_url}/api/auth/google/callback"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def start(settings: Settings, next_path: str) -> tuple[str, str]:
    """Return (Google URL, signed state cookie value)."""
    state = security.new_token()
    verifier = security.new_token() + security.new_token()[:20]
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    payload = _b64(json.dumps({"s": state, "v": verifier, "n": safe_next(next_path),
                               "t": int(time.time())}).encode())
    cookie = f"{payload}.{security.sign(payload, settings.secret_key)}"
    url = AUTH_URL + "?" + urlencode({
        "client_id": settings.google_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "prompt": "select_account",
    })
    return url, cookie


def read_state(settings: Settings, cookie: str | None, state: str | None) -> tuple[str, str]:
    """Check the state cookie against the returned state. Returns (verifier, next path)."""
    fail = AppError("Google login expired or was started in another browser. Please try again.",
                    code="google_state")
    if not cookie or not state or "." not in cookie:
        raise fail
    payload, sig = cookie.rsplit(".", 1)
    if not security.same(sig, security.sign(payload, settings.secret_key)):
        raise fail
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except ValueError as exc:
        raise fail from exc
    too_old = time.time() - int(data.get("t", 0)) > STATE_MAX_AGE_S
    if not security.same(str(data.get("s", "")), state) or too_old:
        raise fail
    return str(data["v"]), safe_next(str(data.get("n", "/app")))


def safe_next(path: str | None) -> str:
    """Only local paths (no open redirects)."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/app"
    return path


def identity_from_tokens(settings: Settings, tokens: dict) -> GoogleIdentity:
    """Read and check the ID token claims (received directly from Google over TLS)."""
    raw = tokens.get("id_token")
    if not isinstance(raw, str) or raw.count(".") != 2:
        raise AppError("Google did not return an ID token.", code="google_failed")
    body = raw.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    if claims.get("iss") not in ISSUERS:
        raise AppError("Unexpected Google token issuer.", code="google_failed")
    if claims.get("aud") != settings.google_client_id:
        raise AppError("Google token is for another app.", code="google_failed")
    if int(claims.get("exp", 0)) < time.time():
        raise AppError("Google token expired.", code="google_failed")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise AppError("Your Google email address is not verified.", code="google_unverified")
    email_addr = repo.normalize_email(claims["email"])
    name = str(claims.get("name") or email_addr.split("@")[0])[:120]
    return GoogleIdentity(sub=str(claims["sub"]), email=email_addr, name=name)


def login_or_signup(db: Session, ident: GoogleIdentity) -> tuple[User, bool]:
    """Find, link or create the user for a Google identity. Returns (user, is_new)."""
    user = repo.get_user_by_google_sub(db, ident.sub)
    if user is not None:
        return user, False
    user = repo.get_user_by_email(db, ident.email)
    if user is not None:
        if user.google_sub and user.google_sub != ident.sub:
            raise AppError("This email is linked to another Google account.", code="google_conflict")
        if not user.email_verified:
            # Someone may have registered this address without owning it: drop that password.
            user.password_hash = None
            repo.delete_user_sessions(db, user.id)
            user.email_verified_at = utcnow()
        user.google_sub = ident.sub
        audit.record(db, "user.google_linked", actor=user.id, target_type="user", target_id=user.id)
        return user, False
    user = repo.add_user(db, email=ident.email, name=ident.name, password_hash=None,
                         google_sub=ident.sub, verified_at=utcnow())
    audit.record(db, "user.signup", actor=user.id, target_type="user", target_id=user.id, method="google")
    return user, True


def get_client() -> GoogleClient:
    """FastAPI dependency (tests replace it with a fake)."""
    return HttpGoogleClient(get_settings())
