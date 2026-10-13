"""CountVision product tables: sites, devices, pairing codes, cameras and the counting data.

Counting data (line_counts, zone_stats, coverage, events) are time series. When the database
has TimescaleDB they become hypertables (migration 0002); without it they are normal tables,
so everything also works on plain PostgreSQL.

Only numbers are stored. There are no images, no video, no track coordinates.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
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
    # Which lines count as "visitors" on the dashboard: [{"camera_id": "<uuid>", "line": "Door"}].
    # None = every line. A shop with a door line and an aisle line must not add both together.
    visitor_lines: Mapped[list[dict[str, str]] | None] = mapped_column(JSONB)

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
    # Config (Phase 3 C). "reported" = what the edge runs now; "desired" = edited in the cloud.
    # The edge applies desired_version and reports applied_version back.
    reported_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    desired_config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    desired_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    applied_version: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    config_error: Mapped[str | None] = mapped_column(String(300))
    snapshots_allowed: Mapped[bool | None]
    frame_width: Mapped[int | None] = mapped_column(Integer)
    frame_height: Mapped[int | None] = mapped_column(Integer)
    # More cameras than the plan allows: the newest ones are paused (their numbers are not stored
    # until the plan is upgraded or another camera is removed). See services/plan_limits.py.
    plan_paused: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))

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

    __table_args__ = (
        PrimaryKeyConstraint("camera_id", "line", "class_name", "window_start"),
        Index("ix_line_counts_camera_ws", "camera_id", "window_start"),  # dashboards read by time
    )


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

    __table_args__ = (
        PrimaryKeyConstraint("camera_id", "zone", "class_name", "window_start"),
        Index("ix_zone_stats_camera_ws", "camera_id", "window_start"),
    )


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


class ReportSchedule(Base):
    """A report that is emailed regularly (PDF) to members of the organization (Phase 4 A).

    Recipients are stored as user ids: a person who leaves the organization stops getting it.
    ``last_period_end`` is the last local date that was sent, so a report is never sent twice and
    a pause (server down) sends only the newest missed report, not a flood.
    """

    __tablename__ = "report_schedules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    frequency: Mapped[str] = mapped_column(String(8))  # daily | weekly | monthly
    weekday: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))  # weekly: send day
    hour: Mapped[int] = mapped_column(Integer, default=7, server_default=text("7"))  # local send hour
    recipients: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    attach_csv: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    last_sent_at: Mapped[datetime | None]
    last_period_end: Mapped[date | None]
    last_error: Mapped[str | None] = mapped_column(String(300))
    failures: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    site: Mapped[Site] = relationship()



# ---------------------------------------------------------------------- Phase 4 B


class NotificationChannel(Base):
    """Where alerts (and webhook events) go: email to members, a Telegram chat, or a webhook URL.

    ``config``: email {"recipients": [user ids]}; telegram {"chat_id", "chat_title"};
    webhook {"url", "events": [...]}. Secrets (bot token, webhook signing secret) are encrypted in
    ``secret_enc`` (saas/crypto.py).
    """

    __tablename__ = "notification_channels"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # email | telegram | webhook
    name: Mapped[str] = mapped_column(String(120))
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    secret_enc: Mapped[str | None] = mapped_column(String(500))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    last_ok_at: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(String(300))
    last_error_at: Mapped[datetime | None]


class AlertRule(Base):
    """A condition on a site that sends a message when it becomes true (and when it is over).

    kind: occupancy (people in an area zone >= threshold for duration_min minutes), queue (people
    waiting in a queue zone), camera_offline (a camera not counting for duration_min minutes),
    traffic_drop (visitors in the last duration_min minutes < threshold % of a normal week).
    """

    __tablename__ = "alert_rules"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    site_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))
    camera_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    zone: Mapped[str | None] = mapped_column(String(64))  # None = all zones of that kind on the site
    threshold: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    duration_min: Mapped[int] = mapped_column(Integer, default=5, server_default=text("5"))
    channel_ids: Mapped[list[str]] = mapped_column(JSONB, default=list, server_default=text("'[]'::jsonb"))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    # evaluation state
    state: Mapped[str] = mapped_column(String(8), default="ok", server_default=text("'ok'"))  # ok | firing
    true_since: Mapped[datetime | None]  # condition true since (not yet long enough = pending)
    last_true_at: Mapped[datetime | None]
    fired_at: Mapped[datetime | None]
    last_value: Mapped[float | None] = mapped_column(Float)
    last_eval_at: Mapped[datetime | None]
    last_message: Mapped[str | None] = mapped_column(String(300))

    site: Mapped[Site] = relationship()


class AlertEvent(Base):
    """History: an alert started (fired) or ended (resolved). Also test messages."""

    __tablename__ = "alert_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    rule_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("alert_rules.id", ondelete="SET NULL"))
    site_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("sites.id", ondelete="SET NULL"))
    rule_name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(8))  # fired | resolved
    value: Mapped[float | None] = mapped_column(Float)
    message: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))

    __table_args__ = (Index("ix_alert_events_org_created", "org_id", "created_at"),)


class ApiKey(Base):
    """Key for the public API (read-only). Only the SHA-256 hash is stored; shown once."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    prefix: Mapped[str] = mapped_column(String(16))  # "cvk_ab12cd" for the list
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class WebhookDelivery(Base):
    """One event sent (or to be sent) to one webhook. Retried with growing pauses. Kept 14 days."""

    __tablename__ = "webhook_deliveries"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"))
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(default=uuid.uuid4)
    event_type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(8), default="pending", server_default=text("'pending'"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    next_try_at: Mapped[datetime | None]
    response_code: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"), index=True)
    delivered_at: Mapped[datetime | None]

    __table_args__ = (Index("ix_webhook_deliveries_due", "status", "next_try_at"),)


class VideoJob(Base):
    """A video file uploaded for analysis (traffic study, event, a camera without network).

    status: uploading -> probing -> ready (draw lines) -> queued -> running -> done | failed |
    cancelled. The uploaded video is deleted when the analysis ends; the annotated video after
    CV_VIDEO_KEEP_DAYS; the numbers (``result``) stay until the job is deleted.
    """

    __tablename__ = "video_jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(12), default="uploading", server_default=text("'uploading'"))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default=text("0"))
    duration_s: Mapped[float | None] = mapped_column(Float)
    fps: Mapped[float | None] = mapped_column(Float)
    frame_count: Mapped[int | None] = mapped_column(Integer)
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    progress: Mapped[float] = mapped_column(Float, default=0, server_default=text("0"))
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=utcnow, server_default=text("now()"))
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    updated_at: Mapped[datetime | None]  # last progress note from the worker (a dead worker is noticed)
    video_deleted_at: Mapped[datetime | None]  # annotated video removed (kept numbers)
    # Plan usage: minutes of video counted against the month in which the analysis was started.
    billed_month: Mapped[str | None] = mapped_column(String(7))  # "2026-10"
    billed_minutes: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

TIMESERIES = {
    # table -> time column (for hypertables and retention)
    "line_counts": "window_start",
    "zone_stats": "window_start",
    "coverage": "window_start",
    "count_events": "ts",
}
