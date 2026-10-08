"""Passwords (Argon2id) and random tokens (stored only as SHA-256)."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # Argon2id with the library's safe defaults

MIN_PASSWORD = 10
MAX_PASSWORD = 200

# A real hash of a random password: used to spend the same time when the email is unknown,
# so the login answer time does not tell an attacker which emails have an account.
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """True when the password matches. Unknown users still cost one hash check."""
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def password_problem(password: str, email: str | None = None) -> str | None:
    """Simple, honest rules: long enough, not the email. Returns the problem or None."""
    if len(password) < MIN_PASSWORD:
        return f"Password must have at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"Password must have at most {MAX_PASSWORD} characters."
    if email and password.strip().lower() == email.strip().lower():
        return "Password must not be your email address."
    if len(set(password)) < 4:
        return "Password is too simple."
    return None


def new_token() -> str:
    """Random URL-safe token (256 bits)."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """SHA-256 hex of a token. Tokens are long and random, so a fast hash is fine."""
    return hashlib.sha256(token.encode()).hexdigest()


def sign(value: str, key: str) -> str:
    """HMAC signature (used for the short-lived Google login state cookie)."""
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)
