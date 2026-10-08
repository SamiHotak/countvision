"""Small fixed-window rate limits in Redis (login attempts, password reset mails, invites).

If Redis is down we log a warning and allow the request: a broken limiter must not lock
everybody out. Keys contain a hash of the email, never the email itself.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging

from fastapi import Request

from ...redis_client import get_redis
from ...settings import get_settings
from ..errors import TooManyRequests

log = logging.getLogger(__name__)

PREFIX = "cv:rl:"


def _key(kind: str, value: str) -> str:
    digest = hashlib.sha256(value.strip().lower().encode()).hexdigest()[:32]
    return f"{PREFIX}{kind}:{digest}"


def count(kind: str, value: str) -> int:
    """Current count in the window (0 when Redis is not reachable)."""
    try:
        raw = get_redis().get(_key(kind, value))
    except Exception:
        log.warning("Rate limiter: Redis not reachable, not limiting", exc_info=True)
        return 0
    return int(raw or 0)


def hit(kind: str, value: str, window_s: int) -> int:
    """Count one event and return the new count."""
    key = _key(kind, value)
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s, nx=True)
        new, _ = pipe.execute()
    except Exception:
        log.warning("Rate limiter: Redis not reachable, not limiting", exc_info=True)
        return 0
    return int(new)


def reset(kind: str, value: str) -> None:
    try:
        get_redis().delete(_key(kind, value))
    except Exception:
        log.warning("Rate limiter: Redis not reachable", exc_info=True)


def limit(kind: str, value: str, max_events: int, window_s: int,
          message: str = "Too many attempts. Please wait a few minutes and try again.") -> None:
    """Count one event and raise 429 when the limit is passed."""
    if hit(kind, value, window_s) > max_events:
        raise TooManyRequests(message)


# Requests reach the API through the Next.js server (and later Caddy). Their address is "trusted":
# for those we read the real client from X-Forwarded-For.
_TRUSTED = [ipaddress.ip_network(n) for n in
            ("127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]


def _trusted(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return any(ip in net for net in _TRUSTED)


def client_ip(request: Request) -> str:
    """The client address: the right-most untrusted entry of X-Forwarded-For, else the peer."""
    peer = request.client.host if request.client else "unknown"
    if not _trusted(peer):
        return peer
    forwarded = request.headers.get("x-forwarded-for", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    for hop in reversed(hops):
        if not _trusted(hop):
            return hop
    return hops[0] if hops else peer


def login_limits() -> tuple[int, int]:
    s = get_settings()
    return s.login_max_attempts, s.login_window_s
