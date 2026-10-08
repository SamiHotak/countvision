"""/api/auth: sign up, log in, log out, email confirmation, password reset, Google login."""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ...db import get_db
from ...settings import get_settings
from ..cookies import clear_session_cookie, set_session_cookie
from ..deps import current_user, session_token
from ..errors import AppError
from ..models import User
from ..schemas import (
    EmailIn,
    LoginIn,
    MeOut,
    OkOut,
    ProvidersOut,
    ResetPasswordIn,
    SignupIn,
    TokenIn,
)
from ..services import auth, google, invites, ratelimit
from .me import me_out

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/providers", response_model=ProvidersOut)
def providers() -> ProvidersOut:
    """Which login methods the login page should show."""
    return ProvidersOut(google=get_settings().google_enabled)


@router.post("/signup", status_code=201, response_model=MeOut)
def signup(body: SignupIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Create an account and log in. With invite_token: join that organization at once."""
    ratelimit.limit("signup-ip", ratelimit.client_ip(request), get_settings().signups_per_ip_hour, 3600)
    if body.invite_token:
        user, _ = invites.signup_and_accept(db, body.invite_token, email_addr=body.email, name=body.name,
                                            password=body.password)
    else:
        user = auth.signup(db, email_addr=body.email, name=body.name, password=body.password)
    token = auth.start_session(db, user)
    db.commit()
    set_session_cookie(response, token)
    return me_out(db, user)


@router.post("/login", response_model=MeOut)
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    user = auth.login(db, email_addr=body.email, password=body.password, ip=ratelimit.client_ip(request))
    old = session_token(request)
    if old:
        auth.logout(db, old)  # new session id after login (no session fixation)
    token = auth.start_session(db, user)
    db.commit()
    set_session_cookie(response, token)
    return me_out(db, user)


@router.post("/logout", response_model=OkOut)
def logout(request: Request, response: Response, db: Session = Depends(get_db)) -> OkOut:
    auth.logout(db, session_token(request))
    db.commit()
    clear_session_cookie(response)
    return OkOut()


@router.post("/logout-all", response_model=OkOut)
def logout_all(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)) -> OkOut:
    """Log out all OTHER browsers and devices."""
    n = auth.logout_everywhere(db, user, keep_token=session_token(request))
    db.commit()
    return OkOut(message=f"Logged out {n} other session(s).")


@router.post("/verify-email", response_model=OkOut)
def verify_email(body: TokenIn, db: Session = Depends(get_db)) -> OkOut:
    auth.verify_email(db, body.token)
    db.commit()
    return OkOut(message="Email confirmed.")


@router.post("/resend-verification", response_model=OkOut)
def resend_verification(user: User = Depends(current_user), db: Session = Depends(get_db)) -> OkOut:
    if user.email_verified:
        return OkOut(message="Your email is already confirmed.")
    ratelimit.limit("verify-user", str(user.id), 5, 3600, "Please wait before asking for another email.")
    auth.send_verification(db, user)
    db.commit()
    return OkOut(message="We sent you a new confirmation email.")


@router.post("/forgot-password", response_model=OkOut)
def forgot_password(body: EmailIn, request: Request, db: Session = Depends(get_db)) -> OkOut:
    auth.forgot_password(db, body.email, ratelimit.client_ip(request))
    db.commit()
    return OkOut(message="If an account exists for this email, we sent a link to reset the password.")


@router.post("/reset-password", response_model=OkOut)
def reset_password(body: ResetPasswordIn, response: Response, db: Session = Depends(get_db)) -> OkOut:
    auth.reset_password(db, body.token, body.password)
    db.commit()
    clear_session_cookie(response)
    return OkOut(message="Password changed. Please log in with the new password.")


# --- Google ---------------------------------------------------------------------------------

@router.get("/google/start")
def google_start(next: str = "/app") -> RedirectResponse:
    s = get_settings()
    if not s.google_enabled:
        raise AppError("Google login is not set up on this server.", code="google_disabled", status=404)
    url, cookie = google.start(s, next)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(google.STATE_COOKIE, cookie, max_age=google.STATE_MAX_AGE_S, httponly=True,
                    secure=s.secure_cookies, samesite="lax", path="/api/auth/google")
    return resp


@router.get("/google/callback")
def google_callback(request: Request, code: str | None = None, state: str | None = None,
                    error: str | None = None, db: Session = Depends(get_db),
                    client: google.GoogleClient = Depends(google.get_client)) -> RedirectResponse:
    """Google sends the browser back here. On errors we redirect to the login page with a message."""
    s = get_settings()

    def fail(message: str) -> RedirectResponse:
        resp = RedirectResponse(f"{s.web_url}/login?error={quote(message)}", status_code=302)
        resp.delete_cookie(google.STATE_COOKIE, path="/api/auth/google")
        return resp

    if not s.google_enabled:
        return fail("Google login is not set up on this server.")
    if error or not code:
        return fail("Google login was cancelled.")
    try:
        verifier, next_path = google.read_state(s, request.cookies.get(google.STATE_COOKIE), state)
        ident = google.identity_from_tokens(s, client.exchange(code, verifier, google.redirect_uri(s)))
        user, _ = google.login_or_signup(db, ident)
        if not user.is_active:
            return fail("This account is disabled.")
        old = session_token(request)
        if old:
            auth.logout(db, old)
        token = auth.start_session(db, user)
        db.commit()
    except AppError as exc:
        db.rollback()
        return fail(exc.message)
    resp = RedirectResponse(f"{s.web_url}{next_path}", status_code=302)
    resp.delete_cookie(google.STATE_COOKIE, path="/api/auth/google")
    set_session_cookie(resp, token)
    return resp
