"""Shared Redis connections: text (rate limits, keys, pub/sub) and bytes (snapshots).

In the local edition (CV_EDITION=local) there is no Redis server: the same calls go to an
in-process store with the same interface (local/store.py).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

import redis
import redis.asyncio as aioredis

from .settings import get_settings


@lru_cache
def get_redis() -> Any:
    """One client per process. Short timeouts: Redis problems must not hang requests."""
    if get_settings().is_local:
        from .local.store import LocalStore

        return LocalStore(decode=True)
    return redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True
    )


@lru_cache
def get_redis_bytes() -> Any:
    """Same server, raw bytes (JPEG snapshots)."""
    if get_settings().is_local:
        from .local.store import LocalStore

        return LocalStore(decode=False)
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=5, socket_connect_timeout=2)


def async_redis() -> Any:
    """A NEW async client (for one long-poll or live stream). Close it with ``await r.aclose()``."""
    if get_settings().is_local:
        from .local.store import AsyncLocalStore

        return AsyncLocalStore(decode=True)
    return aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, decode_responses=True)


def reset_clients() -> None:
    """Forget the cached clients (tests that switch the edition)."""
    get_redis.cache_clear()
    get_redis_bytes.cache_clear()
