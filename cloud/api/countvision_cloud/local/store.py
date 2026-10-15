"""An in-process replacement for Redis (local edition: one API process, no Redis server).

It implements exactly the Redis commands CountVision uses, with the same return types as redis-py:

* keys with optional expiry: get, set (ex, nx), incr, expire (nx), ttl, exists, delete
* hashes: hset, hget, hgetall, hdel
* pipeline() (commands run one after the other under the lock, results as a list)
* publish + async pub/sub (the browser's live stream): publish from ANY thread, the waiting
  asyncio stream is woken with ``loop.call_soon_threadsafe``
* ping, flushdb, close/aclose

Values are kept as bytes, like on a Redis server. A "text" client (``decode=True``) returns str,
a "bytes" client returns bytes (JPEG snapshots). All clients share one ``Server``.

Why our own instead of the fakeredis package: no extra dependency (the cloud Docker image and its
dependency cache stay as they are), only ~250 lines, and cross-thread pub/sub into asyncio, which
is the part the live stream needs, is explicit here.

Data lives in memory only: rate-limit counters, device "something changed" counters, snapshot
requests and the 10-minute snapshot pictures. A restart loses them, which is harmless (devices
get their full config again at the next poll, snapshots can be taken again).
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import Iterable
from typing import Any

_PURGE_EVERY_S = 30.0


def _b(value: Any) -> bytes:
    """Store like Redis: everything becomes bytes (ints and floats as their text)."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, bool):
        raise TypeError("Invalid input of type: 'bool'")
    if isinstance(value, (int, float, str)):
        return str(value).encode()
    raise TypeError(f"Invalid input of type: {type(value).__name__!r}")


class Server:
    """The shared data of all clients in this process."""

    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.data: dict[str, bytes | dict[bytes, bytes]] = {}
        self.expires: dict[str, float] = {}  # key -> time.monotonic() deadline
        self.subscribers: dict[str, set[AsyncPubSub]] = {}
        self._next_purge = 0.0

    # -- expiry --------------------------------------------------------------------------

    def alive(self, key: str) -> bool:
        deadline = self.expires.get(key)
        if deadline is not None and deadline <= time.monotonic():
            self.data.pop(key, None)
            self.expires.pop(key, None)
            return False
        return key in self.data

    def purge(self) -> None:
        now = time.monotonic()
        if now < self._next_purge:
            return
        self._next_purge = now + _PURGE_EVERY_S
        for key in [k for k, d in self.expires.items() if d <= now]:
            self.data.pop(key, None)
            self.expires.pop(key, None)

    # -- pub/sub -------------------------------------------------------------------------

    def publish(self, channel: str, message: bytes) -> int:
        with self.lock:
            targets = list(self.subscribers.get(channel, ()))
        delivered = 0
        for sub in targets:
            if sub.deliver(channel, message):
                delivered += 1
        return delivered

    def subscribe(self, channel: str, sub: AsyncPubSub) -> None:
        with self.lock:
            self.subscribers.setdefault(channel, set()).add(sub)

    def unsubscribe(self, channel: str, sub: AsyncPubSub) -> None:
        with self.lock:
            subs = self.subscribers.get(channel)
            if subs is not None:
                subs.discard(sub)
                if not subs:
                    del self.subscribers[channel]


_SERVER = Server()


def server() -> Server:
    """The process-wide store (one per API process)."""
    return _SERVER


class LocalStore:
    """A synchronous client with the redis-py interface used by CountVision."""

    def __init__(self, decode: bool = True, srv: Server | None = None) -> None:
        self.decode = decode
        self.srv = srv or _SERVER

    def _out(self, value: bytes | None) -> Any:
        if value is None:
            return None
        return value.decode() if self.decode else value

    def _string(self, key: str) -> bytes | None:
        if not self.srv.alive(key):
            return None
        value = self.srv.data[key]
        if isinstance(value, dict):
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return value

    def _hash(self, key: str, create: bool = False) -> dict[bytes, bytes] | None:
        if not self.srv.alive(key):
            if not create:
                return None
            self.srv.data[key] = {}
            self.srv.expires.pop(key, None)
        value = self.srv.data[key]
        if not isinstance(value, dict):
            raise TypeError("WRONGTYPE Operation against a key holding the wrong kind of value")
        return value

    # -- connection ----------------------------------------------------------------------

    def ping(self) -> bool:
        return True

    def close(self) -> None:
        pass

    def flushdb(self) -> bool:
        with self.srv.lock:
            self.srv.data.clear()
            self.srv.expires.clear()
        return True

    # -- strings -------------------------------------------------------------------------

    def get(self, key: str) -> Any:
        with self.srv.lock:
            return self._out(self._string(key))

    def set(self, key: str, value: Any, ex: int | float | None = None, nx: bool = False) -> bool | None:
        with self.srv.lock:
            self.srv.purge()
            if nx and self.srv.alive(key):
                return None
            self.srv.data[key] = _b(value)
            if ex is not None:
                self.srv.expires[key] = time.monotonic() + float(ex)
            else:
                self.srv.expires.pop(key, None)
            return True

    def incr(self, key: str, amount: int = 1) -> int:
        with self.srv.lock:
            current = self._string(key)
            try:
                new = int(current or b"0") + amount
            except ValueError as exc:
                raise ValueError("value is not an integer or out of range") from exc
            self.srv.data[key] = str(new).encode()  # INCR keeps the expiry, like Redis
            return new

    def expire(self, key: str, seconds: int | float, nx: bool = False) -> bool:
        with self.srv.lock:
            if not self.srv.alive(key):
                return False
            if nx and key in self.srv.expires:
                return False
            self.srv.expires[key] = time.monotonic() + float(seconds)
            return True

    def ttl(self, key: str) -> int:
        with self.srv.lock:
            if not self.srv.alive(key):
                return -2
            deadline = self.srv.expires.get(key)
            return -1 if deadline is None else max(0, round(deadline - time.monotonic()))

    def exists(self, *keys: str) -> int:
        with self.srv.lock:
            return sum(1 for k in keys if self.srv.alive(k))

    def delete(self, *keys: str) -> int:
        with self.srv.lock:
            n = 0
            for key in keys:
                if self.srv.alive(key):
                    n += 1
                self.srv.data.pop(key, None)
                self.srv.expires.pop(key, None)
            return n

    # -- hashes --------------------------------------------------------------------------

    def hset(self, name: str, key: Any = None, value: Any = None, mapping: dict | None = None) -> int:
        items: list[tuple[Any, Any]] = []
        if key is not None:
            items.append((key, value))
        if mapping:
            items.extend(mapping.items())
        with self.srv.lock:
            h = self._hash(name, create=True)
            assert h is not None
            added = 0
            for k, v in items:
                kb = _b(k)
                if kb not in h:
                    added += 1
                h[kb] = _b(v)
            return added

    def hget(self, name: str, key: Any) -> Any:
        with self.srv.lock:
            h = self._hash(name)
            return self._out(h.get(_b(key))) if h else None

    def hgetall(self, name: str) -> dict:
        with self.srv.lock:
            h = self._hash(name) or {}
            return {self._out(k): self._out(v) for k, v in h.items()}

    def hdel(self, name: str, *keys: Any) -> int:
        with self.srv.lock:
            h = self._hash(name)
            if not h:
                return 0
            n = 0
            for key in keys:
                if h.pop(_b(key), None) is not None:
                    n += 1
            if not h:
                self.srv.data.pop(name, None)
                self.srv.expires.pop(name, None)
            return n

    # -- pub/sub -------------------------------------------------------------------------

    def publish(self, channel: str, message: Any) -> int:
        return self.srv.publish(channel, _b(message))

    # -- pipeline ------------------------------------------------------------------------

    def pipeline(self, transaction: bool = True) -> Pipeline:
        return Pipeline(self)


class Pipeline:
    """Collects commands; ``execute()`` runs them together under the store's lock."""

    def __init__(self, store: LocalStore) -> None:
        self._store = store
        self._calls: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        if name.startswith("_") or not hasattr(self._store, name):
            raise AttributeError(name)

        def queue(*args: Any, **kwargs: Any) -> Pipeline:
            self._calls.append((name, args, kwargs))
            return self

        return queue

    def execute(self) -> list[Any]:
        with self._store.srv.lock:
            calls, self._calls = self._calls, []
            return [getattr(self._store, name)(*args, **kwargs) for name, args, kwargs in calls]

    def __enter__(self) -> Pipeline:
        return self

    def __exit__(self, *exc: object) -> None:
        self._calls = []


class AsyncLocalStore:
    """The asyncio client (long-poll of devices, live stream). Same data as LocalStore."""

    def __init__(self, decode: bool = True, srv: Server | None = None) -> None:
        self._sync = LocalStore(decode, srv)

    async def get(self, key: str) -> Any:
        return self._sync.get(key)

    async def set(self, key: str, value: Any, ex: int | float | None = None, nx: bool = False) -> Any:
        return self._sync.set(key, value, ex=ex, nx=nx)

    async def incr(self, key: str, amount: int = 1) -> int:
        return self._sync.incr(key, amount)

    async def publish(self, channel: str, message: Any) -> int:
        return self._sync.publish(channel, message)

    async def ping(self) -> bool:
        return True

    def pubsub(self) -> AsyncPubSub:
        return AsyncPubSub(self._sync.srv, self._sync.decode)

    async def aclose(self) -> None:
        pass

    async def close(self) -> None:
        pass


class AsyncPubSub:
    """redis.asyncio PubSub look-alike: subscribe, get_message, unsubscribe, aclose."""

    def __init__(self, srv: Server, decode: bool = True) -> None:
        self._srv = srv
        self._decode = decode
        self._channels: set[str] = set()
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.max_queue = 1000  # a stuck browser must not fill the memory

    def _value(self, raw: bytes | str) -> Any:
        if isinstance(raw, bytes) and self._decode:
            return raw.decode()
        return raw

    async def subscribe(self, *channels: str) -> None:
        if self._queue is None:
            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue()
        for channel in channels:
            self._channels.add(channel)
            self._srv.subscribe(channel, self)
            self._queue.put_nowait({"type": "subscribe", "pattern": None, "channel": channel,
                                    "data": len(self._channels)})

    def deliver(self, channel: str, message: bytes) -> bool:
        """Called by ``publish`` from any thread."""
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            return False
        item = {"type": "message", "pattern": None, "channel": channel, "data": self._value(message)}

        def put() -> None:
            if queue.qsize() < self.max_queue:
                queue.put_nowait(item)

        try:
            loop.call_soon_threadsafe(put)
        except RuntimeError:  # the loop was closed in the meantime
            return False
        return True

    async def get_message(self, ignore_subscribe_messages: bool = False,
                          timeout: float | None = 0.0) -> dict[str, Any] | None:
        if self._queue is None:
            if timeout:
                await asyncio.sleep(timeout)
            return None
        deadline = None if timeout is None else asyncio.get_running_loop().time() + timeout
        while True:
            try:
                if deadline is None:
                    item = await self._queue.get()
                else:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        item = self._queue.get_nowait()
                    else:
                        item = await asyncio.wait_for(self._queue.get(), remaining)
            except (TimeoutError, asyncio.QueueEmpty):
                return None
            if ignore_subscribe_messages and item["type"] in ("subscribe", "unsubscribe"):
                continue
            return item

    async def unsubscribe(self, *channels: str) -> None:
        for channel in list(channels or self._channels):
            self._srv.unsubscribe(channel, self)
            self._channels.discard(channel)

    async def aclose(self) -> None:
        await self.unsubscribe()
        self._queue = None
        self._loop = None

    async def close(self) -> None:
        await self.aclose()

    def __del__(self) -> None:  # a stream that ended without aclose must not keep receiving
        with contextlib.suppress(Exception):
            for channel in list(self._channels):
                self._srv.unsubscribe(channel, self)


def keys_like(srv: Server, prefix: str) -> Iterable[str]:
    """Test helper: live keys with this prefix."""
    with srv.lock:
        return [k for k in list(srv.data) if k.startswith(prefix) and srv.alive(k)]
