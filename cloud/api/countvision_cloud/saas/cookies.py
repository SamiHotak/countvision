"""The login cookie: httpOnly, SameSite=Lax, Secure on HTTPS."""

from __future__ import annotations

from fastapi import Response

from ..settings import get_settings


def set_session_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.session_cookie, token, max_age=s.session_days * 86400, httponly=True,
        secure=s.secure_cookies, samesite="lax", path="/",
    )


def clear_session_cookie(response: Response) -> None:
    s = get_settings()
    response.delete_cookie(s.session_cookie, path="/", secure=s.secure_cookies, httponly=True, samesite="lax")
