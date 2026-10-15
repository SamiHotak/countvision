"""Licence keys for CountVision Local, checked offline.

A licence key is a small JSON document signed with Ezat's Ed25519 PRIVATE key:

    CVL1-<base64url(JSON)>.<base64url(signature)>

    JSON = {"v": 1, "id": "lic_8f3k2m", "customer": "Café Sonne", "cameras": 4,
            "valid_until": "2027-10-31" | null, "updates_until": "2027-10-31" | null,
            "edition": "local", "issued": "2026-10-14"}

The app checks the signature with the PUBLIC key built into the code (licence_key.py). Without the
private key nobody can make a key the app accepts. The private key is never in the repository: it is
created once with ``countvision-cloud licence-keygen`` and stays on Ezat's laptop (+ a backup).

What a licence gives (same limit code as the Stripe plans, services/plan_limits.py):
* no licence: a free trial of CV_TRIAL_DAYS (14) days from the first start, 2 cameras
* a valid licence: its number of cameras; video analysis without a minutes limit (it runs on the
  customer's own PC); valid_until = null means it never ends (a one-time licence)
* trial over / licence ended: the product pages show "add a licence"; counting data keeps being
  stored (nothing is lost), the same as an ended subscription in the cloud
``updates_until`` is shown only (the support/updates period of a one-time licence).

Honest limit: this stops casual copying, not a determined cracker: the check runs on the customer's
PC in readable Python. The clock is protected a little: the highest time ever seen is remembered,
so turning the PC clock back does not extend a trial.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..db import utcnow
from ..saas.errors import AppError
from ..saas.models import AppSetting, Organization, User
from ..saas.plans import Plan
from ..settings import get_settings
from . import licence_key

log = logging.getLogger(__name__)

PREFIX = "CVL1-"
KEY_LICENCE = "licence"
KEY_INSTALL = "install"
KEY_CLOCK = "clock"
CLOCK_SAVE_EVERY = timedelta(minutes=30)
MAX_CAMERAS = 64  # a device reports at most 64 cameras

# Without a licence: the free trial (CV_TRIAL_DAYS, default 14). Video analysis has no minutes
# limit in the local edition (the customer's own computer does the work).
LOCAL_TRIAL = Plan("trial", "Free trial", None, {"cameras": 2, "video_minutes": -1})
NO_LICENCE = Plan("none", "No licence", None, {"cameras": 0, "video_minutes": 0})


class LicenceError(AppError):
    def __init__(self, message: str, code: str = "licence_invalid") -> None:
        super().__init__(message, code=code, status=400, fields={"key": message})


@dataclass(frozen=True)
class Licence:
    id: str
    customer: str
    cameras: int
    valid_until: date | None
    updates_until: date | None
    issued: date | None
    edition: str = "local"

    def expired(self, today: date) -> bool:
        return self.valid_until is not None and today > self.valid_until

    def plan(self) -> Plan:
        return Plan("licence", f"Licence: {self.cameras} camera{'s' if self.cameras != 1 else ''}", None,
                    {"cameras": self.cameras, "video_minutes": -1})

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "customer": self.customer, "cameras": self.cameras,
                "valid_until": self.valid_until.isoformat() if self.valid_until else None,
                "updates_until": self.updates_until.isoformat() if self.updates_until else None,
                "issued": self.issued.isoformat() if self.issued else None, "edition": self.edition}


# --- keys ------------------------------------------------------------------------------------------


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def public_key_text() -> str:
    """The public key that checks licences (base64 of the raw 32 bytes)."""
    s = get_settings()
    if s.licence_public_key and s.environment in ("development", "test"):
        return s.licence_public_key.strip()
    return licence_key.PUBLIC_KEY.strip()


@lru_cache(maxsize=8)
def _public_key(text: str) -> Ed25519PublicKey | None:
    if not text:
        return None
    try:
        return Ed25519PublicKey.from_public_bytes(_unb64(text))
    except ValueError:
        log.error("The built-in licence public key is not valid")
        return None


def new_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def private_key_pem(key: Ed25519PrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def load_private_key(pem: bytes) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("not an Ed25519 private key")
    return key


def public_text_of(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return _b64(raw)


def create_key(private: Ed25519PrivateKey, *, customer: str, cameras: int,
               valid_until: date | None = None, updates_until: date | None = None,
               issued: date | None = None, licence_id: str | None = None) -> str:
    """Sign a new licence key (Ezat's laptop only: needs the private key)."""
    customer = customer.strip()
    if not customer:
        raise ValueError("customer name is empty")
    if not 1 <= cameras <= MAX_CAMERAS:
        raise ValueError(f"cameras must be 1..{MAX_CAMERAS}")
    doc = {
        "v": 1,
        "id": licence_id or "lic_" + secrets.token_hex(5),
        "customer": customer[:120],
        "cameras": int(cameras),
        "valid_until": valid_until.isoformat() if valid_until else None,
        "updates_until": updates_until.isoformat() if updates_until else None,
        "edition": "local",
        "issued": (issued or datetime.now(UTC).date()).isoformat(),
    }
    payload = json.dumps(doc, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode()
    return f"{PREFIX}{_b64(payload)}.{_b64(private.sign(payload))}"


def _date(value: Any) -> date | None:
    return date.fromisoformat(value) if value else None


def parse_key(key: str, public_text: str | None = None) -> Licence:
    """Check a licence key; raises LicenceError with a sentence for the user."""
    key = "".join(str(key).split())  # pasted from an email: line breaks and spaces removed
    if not key.startswith(PREFIX) or "." not in key:
        raise LicenceError("This is not a CountVision licence key. It starts with CVL1-.")
    pub = _public_key(public_text if public_text is not None else public_key_text())
    if pub is None:
        raise LicenceError("This copy of CountVision Local cannot check licences (no public key built in). "
                           "Please contact your supplier.", code="licence_unavailable")
    body, _, sig = key[len(PREFIX):].partition(".")
    try:
        payload, signature = _unb64(body), _unb64(sig)
        pub.verify(signature, payload)
        doc = json.loads(payload)
    except (InvalidSignature, ValueError):
        raise LicenceError("This licence key is not valid. Copy the whole key exactly as you received it.") \
            from None
    try:
        lic = Licence(id=str(doc["id"]), customer=str(doc["customer"]), cameras=int(doc["cameras"]),
                      valid_until=_date(doc.get("valid_until")),
                      updates_until=_date(doc.get("updates_until")),
                      issued=_date(doc.get("issued")), edition=str(doc.get("edition", "local")))
    except (KeyError, TypeError, ValueError):
        raise LicenceError("This licence key is damaged.") from None
    if doc.get("v") != 1 or lic.edition != "local":
        raise LicenceError("This licence key is for another CountVision edition.")
    if not 1 <= lic.cameras <= MAX_CAMERAS:
        raise LicenceError("This licence key is damaged.")
    return lic


# --- installation state ------------------------------------------------------------------------------


def _get(db: Session, key: str) -> dict[str, Any] | None:
    row = db.get(AppSetting, key)
    return dict(row.value) if row else None


def _put(db: Session, key: str, value: dict[str, Any]) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def installed_at(db: Session) -> datetime:
    """When this installation started (the trial counts from here). Created on first use."""
    data = _get(db, KEY_INSTALL)
    if data and data.get("installed_at"):
        return datetime.fromisoformat(data["installed_at"])
    # INSERT ... ON CONFLICT: two requests at the same moment must not both create it
    db.execute(pg_insert(AppSetting).values(
        key=KEY_INSTALL, value={"installed_at": utcnow().isoformat(), "id": uuid.uuid4().hex},
    ).on_conflict_do_nothing(index_elements=["key"]))
    value = db.scalar(select(AppSetting.value).where(AppSetting.key == KEY_INSTALL)) or {}
    return datetime.fromisoformat(value["installed_at"])


def clock(db: Session) -> datetime:
    """Now, but never earlier than the highest time this installation has seen."""
    now = utcnow()
    data = _get(db, KEY_CLOCK)
    seen = datetime.fromisoformat(data["max"]) if data and data.get("max") else None
    return max(now, seen) if seen else now


def note_clock(db: Session) -> None:
    """Remember the current time (called by the timers; written at most every 30 minutes)."""
    now = utcnow()
    data = _get(db, KEY_CLOCK)
    seen = datetime.fromisoformat(data["max"]) if data and data.get("max") else None
    if seen is None or now - seen >= CLOCK_SAVE_EVERY:
        _put(db, KEY_CLOCK, {"max": now.isoformat()})


def current(db: Session) -> tuple[Licence | None, str | None]:
    """The stored licence (checked again every time), or (None, why it is not usable)."""
    data = _get(db, KEY_LICENCE)
    if not data or not data.get("key"):
        return None, None
    try:
        return parse_key(data["key"]), None
    except LicenceError as exc:
        return None, exc.message


def entitlement(db: Session):
    """The effective plan of the installation (same Entitlement as the Stripe billing)."""
    from ..saas.services.billing import Entitlement

    now = clock(db)
    trial_ends = installed_at(db) + timedelta(days=get_settings().trial_days)
    lic, _ = current(db)
    if lic is not None:
        valid_until = (datetime.combine(lic.valid_until, datetime.max.time(), tzinfo=UTC)
                       if lic.valid_until else None)
        if not lic.expired(now.date()):
            return Entitlement("active", lic.plan(), True, trial_ends_at=trial_ends, period_end=valid_until)
        if trial_ends > now:
            return Entitlement("trial", LOCAL_TRIAL, True, trial_ends_at=trial_ends, period_end=valid_until)
        return Entitlement("licence_expired", NO_LICENCE, False, trial_ends_at=trial_ends,
                           period_end=valid_until)
    if trial_ends > now:
        return Entitlement("trial", LOCAL_TRIAL, True, trial_ends_at=trial_ends)
    return Entitlement("expired", NO_LICENCE, False, trial_ends_at=trial_ends)


def summary(db: Session, org_id: uuid.UUID) -> dict[str, Any]:
    """Everything the Licence page shows."""
    from ..saas.services import billing

    ent = entitlement(db)
    lic, problem = current(db)
    now = clock(db)
    days_left = None
    if ent.state == "trial" and ent.trial_ends_at:
        days_left = max(0, -(-int((ent.trial_ends_at - now).total_seconds()) // 86400))
    return {
        "state": ent.state,
        "active": ent.active,
        "plan": billing.plan_out(ent.plan),
        "trial_ends_at": ent.trial_ends_at.isoformat() if ent.trial_ends_at else None,
        "trial_days_left": days_left,
        "trial_plan": billing.plan_out(LOCAL_TRIAL),
        "licence": lic.as_dict() if lic else None,
        "licence_problem": problem,
        "usage": billing.usage(db, org_id),
        "installed_at": installed_at(db).isoformat(),
        "can_check": _public_key(public_key_text()) is not None,
    }


def install(db: Session, org: Organization, actor: User, key: str) -> Licence:
    """Check and save a licence key (replaces an older one)."""
    from ..saas.services import audit, billing

    lic = parse_key(key)
    if lic.expired(clock(db).date()):
        raise LicenceError(f"This licence ended on {lic.valid_until}. Ask for a new key.",
                           code="licence_expired")
    _put(db, KEY_LICENCE, {"key": "".join(key.split()), "added_at": utcnow().isoformat(),
                           "added_by": str(actor.id)})
    audit.record(db, "licence.added", actor=actor.id, org_id=org.id, target_type="org", target_id=org.id,
                 licence_id=lic.id, customer=lic.customer, cameras=lic.cameras,
                 valid_until=lic.valid_until.isoformat() if lic.valid_until else None)
    billing.plan_changed(db, org.id)
    return lic


def remove(db: Session, org: Organization, actor: User) -> None:
    from ..saas.services import audit, billing

    row = db.get(AppSetting, KEY_LICENCE)
    if row is None:
        return
    old, _ = current(db)
    db.delete(row)
    db.flush()
    audit.record(db, "licence.removed", actor=actor.id, org_id=org.id, target_type="org", target_id=org.id,
                 licence_id=old.id if old else None)
    billing.plan_changed(db, org.id)


def all_org_ids(db: Session) -> set[uuid.UUID]:
    return set(db.scalars(select(Organization.id)))
