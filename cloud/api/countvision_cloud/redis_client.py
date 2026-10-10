"""Shared Redis connections: text (rate limits, keys, pub/sub) and bytes (snapshots)."""

from __future__ import annotations

from functools import lru_cache

import redis
import redis.asyncio as aioredis

from .settings import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    """One client per process. Short timeouts: Redis problems must not hang requests."""
    return redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True
    )


@lru_cache
def get_redis_bytes() -> redis.Redis:
    """Same server, raw bytes (JPEG snapshots)."""
    return redis.Redis.from_url(get_settings().redis_url, socket_timeout=5, socket_connect_timeout=2)


def async_redis() -> aioredis.Redis:
    """A NEW async client (for one long-poll or live stream). Close it with ``await r.aclose()``."""
    return aioredis.Redis.from_url(get_settings().redis_url, socket_connect_timeout=2, decode_responses=True)
