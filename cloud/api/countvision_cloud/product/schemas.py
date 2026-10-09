"""Request/response models for sites, devices, cameras and the device ingest API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any
from zoneinfo import available_timezones

from pydantic import BaseModel, ConfigDict, Field, field_validator

_EDGE_ID = r"^[A-Za-z0-9_.:-]{1,64}$"
_NAME = r"^[^\x00-\x1f]{1,64}$"  # line / zone / class names: printable, short


def _clean(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("Must not be empty")
    return value


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- sites ----------------------------------------------------------------------------------

_TIMEZONES = available_timezones()


class SiteIn(_In):
    name: str = Field(min_length=1, max_length=120)
    timezone: str = "Europe/Berlin"
    address: str | None = Field(default=None, max_length=240)

    _name = field_validator("name")(_clean)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        if value not in _TIMEZONES:
            raise ValueError("Unknown time zone (use a name like Europe/Berlin)")
        return value


class SitePatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    timezone: str | None = None
    address: str | None = Field(default=None, max_length=240)

    @field_validator("name")
    @classmethod
    def _n(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        if value is not None and value not in _TIMEZONES:
            raise ValueError("Unknown time zone (use a name like Europe/Berlin)")
        return value


class SiteOut(BaseModel):
    id: uuid.UUID
    name: str
    timezone: str
    address: str | None
    created_at: datetime
    device_count: int
    camera_count: int


# --- pairing ----------------------------------------------------------------------------------

class PairingCodeIn(_In):
    site_id: uuid.UUID
    device_name: str = Field(min_length=1, max_length=120)

    _name = field_validator("device_name")(_clean)


class PairingCodeOut(BaseModel):
    id: uuid.UUID
    code: str | None  # only in the answer that creates it
    site_id: uuid.UUID
    device_name: str
    expires_at: datetime
    status: str  # pending | used | expired
    device_id: uuid.UUID | None


class PairIn(_In):
    """Sent by the edge agent: countvision-edge pair --code XXXX-XXXX."""

    code: str = Field(min_length=4, max_length=32)
    edge_device_id: str = Field(pattern=_EDGE_ID)
    agent_version: str | None = Field(default=None, max_length=32)


class PairOut(BaseModel):
    device_id: uuid.UUID
    token: str
    org_name: str
    site_name: str
    device_name: str


# --- devices and cameras ------------------------------------------------------------------------

class CameraOut(BaseModel):
    id: uuid.UUID
    edge_camera_id: str
    name: str
    state: str | None
    connected: bool | None
    fps: float | None
    reconnects: int | None
    alert: str | None
    lines: list[str]
    zones: list[str]
    last_seen_at: datetime | None
    today: dict[str, dict[str, int]] = {}  # line -> {"in": n, "out": n}


class DeviceOut(BaseModel):
    id: uuid.UUID
    name: str
    site_id: uuid.UUID
    site_name: str
    edge_device_id: str
    agent_version: str | None
    online: bool
    last_seen_at: datetime | None
    last_data_at: datetime | None
    paired_at: datetime
    revoked: bool
    camera_count: int
    cameras_online: int
    detector: dict[str, Any] | None = None
    upload: dict[str, Any] | None = None


class DeviceDetailOut(DeviceOut):
    cameras: list[CameraOut]
    site_timezone: str
    batches_24h: int
    rows_24h: int


class DevicePatch(_In):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    site_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def _n(cls, value: str | None) -> str | None:
        return None if value is None else _clean(value)


class CameraPatch(_In):
    name: str = Field(min_length=1, max_length=120)

    _name = field_validator("name")(_clean)


# --- ingest (edge -> cloud) ---------------------------------------------------------------------
# Row models IGNORE unknown keys, so a newer agent can send extra fields to an older cloud.

class _Row(BaseModel):
    model_config = ConfigDict(extra="ignore")


class CameraStatusIn(_Row):
    id: str = Field(pattern=_EDGE_ID)
    name: str | None = Field(default=None, max_length=120)
    state: str | None = Field(default=None, max_length=16)
    connected: bool | None = None
    fps: float | None = Field(default=None, ge=0, le=1000)
    reconnects: int | None = Field(default=None, ge=0)
    alert: str | None = Field(default=None, max_length=200)
    lines: list[str] = Field(default_factory=list, max_length=64)
    zones: list[str] = Field(default_factory=list, max_length=64)


class LineCountIn(_Row):
    camera_id: str = Field(pattern=_EDGE_ID)
    window_start: int
    line: str = Field(pattern=_NAME)
    class_name: str = Field(pattern=_NAME)
    in_count: int = Field(ge=0, le=1_000_000)
    out_count: int = Field(ge=0, le=1_000_000)


class ZoneStatIn(_Row):
    camera_id: str = Field(pattern=_EDGE_ID)
    window_start: int
    zone: str = Field(pattern=_NAME)
    class_name: str = Field(pattern=_NAME)
    sample_s: float = Field(ge=0, le=3600)
    occ_avg: float = Field(ge=0)
    occ_max: int = Field(ge=0)
    occ_last: int = Field(ge=0)
    queue_avg: float = Field(ge=0)
    queue_max: int = Field(ge=0)
    visits: int = Field(ge=0)
    dwell_sum_s: float = Field(ge=0)
    dwell_max_s: float = Field(ge=0)


class CoverageIn(_Row):
    camera_id: str = Field(pattern=_EDGE_ID)
    window_start: int
    frames: int = Field(ge=0)
    seconds: float = Field(ge=0, le=3600)


class EventIn(_Row):
    event_id: str = Field(min_length=1, max_length=64)
    camera_id: str = Field(pattern=_EDGE_ID)
    kind: str = Field(max_length=32)
    ts: float
    name: str = Field(pattern=_NAME)
    class_name: str | None = Field(default=None, max_length=32)
    direction: str | None = Field(default=None, max_length=8)
    dwell_s: float | None = None
    speed_kmh: float | None = None


class HeartbeatIn(_Row):
    camera_id: str = Field(pattern=_EDGE_ID)
    ts: float
    fps: float | None = None
    connected: bool | None = None
    reconnects: int | None = None
    cpu_pct: float | None = None
    mem_pct: float | None = None
    gpu_util_pct: float | None = None


class IngestIn(BaseModel):
    """One upload from the edge. Every list is optional; an empty batch is a heartbeat."""

    model_config = ConfigDict(extra="ignore")

    batch_id: str = Field(min_length=1, max_length=64)
    sent_at: float | None = None
    agent_version: str | None = Field(default=None, max_length=32)
    status: dict[str, Any] | None = None  # detector, privacy, upload backlog ... (no cameras)
    cameras: list[CameraStatusIn] = Field(default_factory=list, max_length=64)
    line_counts: list[LineCountIn] = Field(default_factory=list, max_length=5000)
    zone_stats: list[ZoneStatIn] = Field(default_factory=list, max_length=5000)
    coverage: list[CoverageIn] = Field(default_factory=list, max_length=5000)
    events: list[EventIn] = Field(default_factory=list, max_length=5000)
    heartbeats: list[HeartbeatIn] = Field(default_factory=list, max_length=500)


class IngestOut(BaseModel):
    accepted: dict[str, int]
    rejected: int
    server_time: float
    duplicate_batch: bool
