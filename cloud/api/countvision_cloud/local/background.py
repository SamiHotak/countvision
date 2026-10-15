"""Background work inside the API process (local edition: no Celery worker, no beat).

* ``submit(fn, *args)``: run something soon in a small thread pool (emails, Telegram/webhook
  messages). The request that caused it does not wait.
* ``Scheduler``: the timers Celery beat runs in the cloud (same task functions, same intervals):
  alerts every minute, webhook retries every minute, reports and hourly webhooks every 5 minutes,
  cleanup every hour, and a heartbeat so /api/health can show "worker: ok".

Started and stopped with the API (main.py lifespan) when CV_EDITION=local and
CV_LOCAL_BACKGROUND is not false. Every task runs inside try/except: one failing task must never
stop the timers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cv-bg")
        return _pool


def submit(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Future:
    """Run ``fn`` in the background thread pool."""

    def run() -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            log.exception("Background job %s failed", getattr(fn, "__name__", fn))
            raise

    return _executor().submit(run)


def shutdown(wait: bool = True) -> None:
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=wait, cancel_futures=not wait)


@dataclass
class Timer:
    name: str
    every_s: float
    fn: Callable[[], Any]
    first_after_s: float = 5.0
    next_at: float = 0.0
    runs: int = 0
    failures: int = 0
    last_error: str | None = field(default=None, repr=False)


def default_timers() -> list[Timer]:
    """The cloud's Celery beat schedule (celery_app.py), as in-process timers."""
    from .. import tasks

    return [
        Timer("heartbeat", 60, tasks.heartbeat, first_after_s=0.5),
        Timer("evaluate-alerts", 60, tasks.evaluate_alerts, first_after_s=20),
        Timer("deliver-due-webhooks", 60, tasks.deliver_due_webhooks, first_after_s=30),
        Timer("send-due-reports", 300, tasks.send_due_reports, first_after_s=60),
        Timer("hourly-webhooks", 300, tasks.hourly_webhooks, first_after_s=90),
        Timer("cleanup", 3600, tasks.cleanup, first_after_s=120),
        Timer("licence-clock", 600, note_clock, first_after_s=10),
    ]


def note_clock() -> None:
    """Remember the highest time seen (turning the PC clock back must not extend the trial)."""
    from ..db import db_session
    from . import licence

    with db_session() as db:
        licence.note_clock(db)


class Scheduler(threading.Thread):
    """Runs every timer at its interval, one at a time (like a single beat + worker)."""

    def __init__(self, timers: list[Timer] | None = None) -> None:
        super().__init__(name="cv-timers", daemon=True)
        self.timers = timers if timers is not None else default_timers()
        self.stop_event = threading.Event()
        now = time.monotonic()
        for t in self.timers:
            t.next_at = now + t.first_after_s

    def run(self) -> None:
        log.info("Background timers started: %s", ", ".join(t.name for t in self.timers))
        while not self.stop_event.is_set():
            now = time.monotonic()
            due = [t for t in self.timers if t.next_at <= now]
            for timer in sorted(due, key=lambda t: t.next_at):
                if self.stop_event.is_set():
                    break
                self._run(timer)
            nxt = min((t.next_at for t in self.timers), default=now + 1.0)
            self.stop_event.wait(max(0.05, min(1.0, nxt - time.monotonic())))
        log.info("Background timers stopped")

    def _run(self, timer: Timer) -> None:
        started = time.monotonic()
        try:
            timer.fn()
            timer.last_error = None
        except Exception as exc:  # noqa: BLE001 - the timers must keep running
            timer.failures += 1
            timer.last_error = f"{type(exc).__name__}: {exc}"[:300]
            log.exception("Background task %s failed", timer.name)
        timer.runs += 1
        timer.next_at = started + timer.every_s
        took = time.monotonic() - started
        if took > 10:
            log.warning("Background task %s took %.1f s", timer.name, took)

    def stop(self, timeout: float = 10.0) -> None:
        self.stop_event.set()
        if self.is_alive():
            self.join(timeout)


_scheduler: Scheduler | None = None


def start() -> Scheduler:
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = Scheduler()
        _scheduler.start()
    return _scheduler


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.stop()
        _scheduler = None
    shutdown(wait=False)


def status() -> list[dict[str, Any]]:
    """For the health check / support: when each timer ran and its last error."""
    if _scheduler is None:
        return []
    now = time.monotonic()
    return [{"name": t.name, "runs": t.runs, "failures": t.failures, "last_error": t.last_error,
             "next_in_s": round(max(0.0, t.next_at - now))} for t in _scheduler.timers]
