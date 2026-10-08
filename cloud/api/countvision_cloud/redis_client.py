"""Shared Redis connection (rate limits now; live counters and caches later)."""

from __future__ import annotations

from functools import lru_cache

import redis

from .settings import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    """One client per process. Short timeouts: Redis problems must not hang requests."""
    return redis.Redis.from_url(
        get_settings().redis_url, socket_timeout=2, socket_connect_timeout=2, decode_responses=True
    )
