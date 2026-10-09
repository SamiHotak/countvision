"""CountVision product tables: sites, devices, pairing codes, cameras and the counting data.

Counting data (line_counts, zone_stats, coverage, events) are time series. When the database
has TimescaleDB they become hypertables (migration 0002); without it they are normal tables,
so everything also works on plain PostgreSQL.

Only numbers are stored. There are no images, no video, no track coordinates.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..db import Base, utcnow


class Site(Base):
    """A place with cameras: a shop, a café, a car park."""

    __tablename__ = "sites"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    timezone: Mapped[str] = mapped_column(String(64), default="Europe/Berlin",
                                          server_default=text("'Europe/Berlin'"))
    address: Mapped[str | None] = mapped_column(String(240))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    devices: Mapped[list[Device]] = relationship(back_populates="site")


class PairingCode(Base):
    """A short one-time code typed on the edge device to connect it to an organization."""

    __tablename__ = "pairing_codes"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"))
    device_name: Mapped[str] = mapped_column(String(120))
    code_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    expires_at: Mapped[datetime]
    used_at: Mapped[datetime | None]
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id", ondelete="SET NULL"))


class Device(Base):
    """An edge computer on site (laptop, mini PC, Jetson). Authenticates with its own token."""

    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id", ondelete="RESTRICT"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    edge_device_id: Mapped[str] = mapped_column(String(64))  # the id the agent reports (edge-xxxx)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    agent_version: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    paired_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    last_seen_at: Mapped[datetime | None]
    last_data_at: Mapped[datetime | None]  # newest counting data received (edge time)
    revoked_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    site: Mapped[Site] = relationship(back_populates="devices")
    cameras: Mapped[list[Camera]] = relationship(back_populates="device", order_by="Camera.edge_camera_id")


class Camera(Base):
    """A camera as reported by a device. Created automatically the first time it reports."""

    __tablename__ = "cameras"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"))
    edge_camera_id: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(120))
    name_locked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # live state from the newest status/heartbeat
    state: Mapped[str | None] = mapped_column(String(16))
    connected: Mapped[bool | None]
    fps: Mapped[float | None] = mapped_column(Float)
    reconnects: Mapped[int | None] = mapped_column(Integer)
    alert: Mapped[str | None] = mapped_column(String(200))
    lines: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    zones: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    last_seen_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    device: Mapped[Device] = relationship(back_populates="cameras")

    __table_args__ = (UniqueConstraint("device_id", "edge_camera_id", name="uq_cameras_device_edge_id"),)


# ---------------------------------------------------------------------- time series
# The primary keys contain the time column (required for TimescaleDB hypertables).
# Ingest uses INSERT ... ON CONFLICT on these keys, so sending a batch twice changes nothing.


class LineCount(Base):
    """IN/OUT per line, class and minute. class_name '*' = all classes together."""

    __tablename__ = "line_counts"

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    window_start: Mapped[datetime]
    line: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(32))
    in_count: Mapped[int] = mapped_column(Integer)
    out_count: Mapped[int] = mapped_column(Integer)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    __table_args__ = (PrimaryKeyConstraint("camera_id", "line", "class_name", "window_start"),)


class ZoneStat(Base):
    """Occupancy, queue and dwell per zone, class and minute."""

    __tablename__ = "zone_stats"

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    window_start: Mapped[datetime]
    zone: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str] = mapped_column(String(32))
    sample_s: Mapped[float] = mapped_column(Float)
    occ_avg: Mapped[float] = mapped_column(Float)
    occ_max: Mapped[int] = mapped_column(Integer)
    occ_last: Mapped[int] = mapped_column(Integer)
    queue_avg: Mapped[float] = mapped_column(Float)
    queue_max: Mapped[int] = mapped_column(Integer)
    visits: Mapped[int] = mapped_column(Integer)
    dwell_sum_s: Mapped[float] = mapped_column(Float)
    dwell_max_s: Mapped[float] = mapped_column(Float)
    received_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    __table_args__ = (PrimaryKeyConstraint("camera_id", "zone", "class_name", "window_start"),)


class Coverage(Base):
    """How many seconds of video were analysed per camera and minute (gaps = camera down)."""

    __tablename__ = "coverage"

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    window_start: Mapped[datetime]
    frames: Mapped[int] = mapped_column(Integer)
    seconds: Mapped[float] = mapped_column(Float)

    __table_args__ = (PrimaryKeyConstraint("camera_id", "window_start"),)


class CountEvent(Base):
    """Single events: line crossings, zone visits, camera offline/online alerts."""

    __tablename__ = "count_events"

    camera_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    ts: Mapped[datetime]
    event_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(64))
    class_name: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[str | None] = mapped_column(String(8))
    dwell_s: Mapped[float | None] = mapped_column(Float)
    speed_kmh: Mapped[float | None] = mapped_column(Float)

    __table_args__ = (
        PrimaryKeyConstraint("camera_id", "event_id", "ts"),
        Index("ix_count_events_camera_ts", "camera_id", "ts"),
    )


class IngestBatch(Base):
    """Small log of received batches (for the device page and debugging). Kept 14 days."""

    __tablename__ = "ingest_batches"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), index=True)
    batch_id: Mapped[str] = mapped_column(String(64))
    received_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"), index=True)
    rows: Mapped[int] = mapped_column(Integer)
    rejected: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (UniqueConstraint("device_id", "batch_id", name="uq_ingest_batches_device_batch"),)


TIMESERIES = {
    # table -> time column (for hypertables and retention)
    "line_counts": "window_start",
    "zone_stats": "window_start",
    "coverage": "window_start",
    "count_events": "ts",
}
