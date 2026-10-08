"""Database engine, session factory and the declarative base."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .settings import get_settings

log = logging.getLogger(__name__)

# Stable constraint names, so Alembic migrations stay readable and predictable.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class of all ORM models."""

    metadata = MetaData(naming_convention=NAMING)
    type_annotation_map = {datetime: DateTime(timezone=True)}


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC (the database stores timestamptz)."""
    return datetime.now(UTC)


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    """The process-wide engine (created on first use)."""
    global _engine, _factory
    if _engine is None:
        url = get_settings().database_url
        _engine = create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10)
        _factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def reset_engine() -> None:
    """Drop the engine (tests switch databases)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None
    return _factory


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, rolled back on errors."""
    db = session_factory()()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def db_session() -> Iterator[Session]:
    """Session for scripts and Celery tasks: commits on success."""
    db = session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
