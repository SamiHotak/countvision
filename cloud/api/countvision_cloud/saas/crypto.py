"""Encrypt small secrets at rest (Telegram bot tokens, webhook signing secrets).

Fernet (AES-128-CBC + HMAC-SHA256, from the ``cryptography`` package, Apache-2.0/BSD) with a key
derived from CV_SECRET_KEY. A database dump alone does not reveal the secrets.
If CV_SECRET_KEY changes, stored secrets can no longer be read: the channels must be set up again
(the web page then shows "secret could not be read").
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from ..settings import get_settings


def _fernet() -> Fernet:
    digest = hashlib.sha256(b"countvision-secrets-v1:" + get_settings().secret_key.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str | None) -> str | None:
    """The plain value, or None when it cannot be read (empty, or another secret key)."""
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
