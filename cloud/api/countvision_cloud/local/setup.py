"""First start of CountVision Local, and the built-in edge agent.

First start (the web app shows a wizard while no account exists):
1. owner account (email + password; no email confirmation: there may be no mail server)
2. business name (= the organization; the local edition has exactly one)
3. first site with its time zone
4. the built-in device "This computer" is created and paired automatically: the API writes the
   device token into the edge agent's folder (cloud.json) together with edge.yaml (which also holds
   the key of the live camera picture, see live_view.py). The launcher
   starts ``countvision-edge run --config <edge_dir>/edge.yaml``; it counts the cameras that are
   added in the web app (managed cameras), none at first.
5. the first camera (webcam / IP camera) is added in the next wizard step with the normal camera API.

At every API start ``ensure_edge_files`` checks that the edge agent's token still matches the
database (after a restore on another PC, or when the edge folder was deleted) and writes a new one
if not. The edge agent picks the new token up by itself (it re-reads cloud.json after a 401).
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import stat
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import __version__
from ..db import utcnow
from ..product.models import Device, Site
from ..product.services import devices as device_service
from ..product.services import sites as site_service
from ..saas import security
from ..saas.errors import Conflict
from ..saas.models import Organization, User
from ..saas.services import audit, auth, orgs
from ..settings import get_settings

log = logging.getLogger(__name__)

BUILTIN_NAME = "This computer"
EDGE_CONFIG = "edge.yaml"
CREDENTIALS = "cloud.json"
DEVICE_ID_FILE = "device_id"


def needs_setup(db: Session) -> bool:
    """True until the first account exists."""
    return (db.scalar(select(func.count(User.id))) or 0) == 0


def check_can_create_org(db: Session) -> None:
    if db.scalar(select(func.count(Organization.id))):
        raise Conflict("CountVision Local manages one business. Add more sites to it instead.",
                       code="local_one_org")


def run(db: Session, *, name: str, email: str, password: str, business: str, site_name: str,
        timezone: str) -> tuple[User, Organization, Site, Device]:
    """The wizard's first step (one transaction). Refused once an account exists."""
    if not needs_setup(db):
        raise Conflict("CountVision is already set up. Please log in.", code="already_set_up")
    user = auth.signup(db, email_addr=email, name=name, password=password, verified=True)
    org = orgs.create_org(db, user, business)
    site = site_service.create_site(db, org, user, name=site_name, timezone=timezone, address=None)
    device = create_builtin(db, org, site, actor=user)
    from . import licence

    licence.installed_at(db)  # the free trial starts now
    audit.record(db, "local.setup_done", actor=user.id, org_id=org.id, target_type="org", target_id=org.id,
                 version=__version__)
    return user, org, site, device


# --- the built-in edge agent ------------------------------------------------------------------------


def builtin_device(db: Session) -> Device | None:
    return db.scalar(select(Device).where(Device.builtin.is_(True), Device.revoked_at.is_(None))
                     .order_by(Device.created_at).limit(1))


def create_builtin(db: Session, org: Organization, site: Site, *, actor: User | None) -> Device:
    """Create the device of this PC and write its token for the edge agent."""
    edge_id = edge_device_id(get_settings().edge_dir)
    token = device_service.TOKEN_PREFIX + secrets.token_urlsafe(32)
    now = utcnow()
    device = Device(org_id=org.id, site_id=site.id, name=BUILTIN_NAME, edge_device_id=edge_id,
                    token_hash=security.token_hash(token), agent_version=None, paired_at=now,
                    manages_cameras=True, builtin=True)
    db.add(device)
    db.flush()
    audit.record(db, "device.paired", actor=actor.id if actor else None, org_id=org.id, target_type="device",
                 target_id=device.id, name=device.name, site=site.name, edge_device_id=edge_id, builtin=True)
    write_edge_files(device, token, org_name=org.name, site_name=site.name)
    return device


def ensure_edge_files(db: Session) -> bool:
    """API start: make sure the edge agent's token works. Returns True when a new one was written."""
    device = builtin_device(db)
    if device is None:
        return False
    path = get_settings().edge_dir / CREDENTIALS
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token", "")
    except (OSError, ValueError):
        token = ""
    edge_dir = get_settings().edge_dir
    config = edge_dir / EDGE_CONFIG
    if token and security.token_hash(token) == device.token_hash:
        want = edge_config_text(edge_dir)
        try:
            same = config.read_text(encoding="utf-8") == want
        except OSError:
            same = False
        if not same:  # an update changed the settings (model path, API address ...): token stays
            _write_private(config, want)
            log.info("Built-in edge agent: %s updated", config)
        return False
    new = device_service.TOKEN_PREFIX + secrets.token_urlsafe(32)
    device.token_hash = security.token_hash(new)
    org = db.get(Organization, device.org_id)
    write_edge_files(device, new, org_name=org.name if org else "", site_name=device.site.name)
    audit.record(db, "device.token_renewed", actor=None, org_id=device.org_id, target_type="device",
                 target_id=device.id, name=device.name)
    log.info("Built-in edge agent: new device token written to %s", path)
    return True


def edge_device_id(edge_dir: Path) -> str:
    """The id the edge agent uses for itself (same file as countvision_edge.config.device_id_for)."""
    path = edge_dir / DEVICE_ID_FILE
    with suppress(OSError):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    edge_dir.mkdir(parents=True, exist_ok=True)
    value = f"edge-{uuid.uuid4().hex[:12]}"
    path.write_text(value + "\n", encoding="utf-8")
    return value


def _q(value: str) -> str:
    """A YAML string (JSON strings are valid YAML; backslashes of Windows paths are escaped)."""
    return json.dumps(value)


def detector_yaml() -> list[str]:
    s = get_settings()
    if s.video_detector == "blobs":  # tests and the synthetic demo only
        return ["detector:", "  type: blobs"]
    model = str(Path(s.video_model).resolve())
    return ["detector:", f"  type: {s.video_detector}", f"  model: {_q(model)}",
            f"  model_license: {_q('Apache-2.0 (Megvii YOLOX)')}", "  conf: 0.2",
            f"  device: {_q(s.video_device)}"]


def edge_config_text(edge_dir: Path) -> str:
    """edge.yaml for the built-in agent. Rewritten at setup and when the token is renewed."""
    s = get_settings()
    lines = [
        "# CountVision Local: settings of the built-in edge agent on this computer.",
        "# Written by the CountVision app - changes here are overwritten. Cameras, lines and zones",
        "# are managed in the web app (Devices -> This computer).",
        f"data_dir: {_q(str(edge_dir))}",
        *detector_yaml(),
        "cloud:",
        "  enabled: true",
        f"  url: {_q(s.local_api_url)}",
        "  upload_interval_s: 5",
        "  manage_cameras: true",
        "privacy:",
        "  preview: blur",
        "  snapshots: true",
        "# Live camera picture for the CountVision app on this computer only (127.0.0.1). Who may",
        "# watch, and whether people are blurred, is set by the owner in Settings -> Privacy.",
        "live:",
        "  enabled: true",
        "  host: 127.0.0.1",
        f"  port: {s.local_live_port}",
        f"  key: {_q(live_key())}",
        "  max_mode: full",
        "storage:",
        "  retention_days: 30",
        "cameras: []",
        "",
    ]
    return "\n".join(lines)


def live_key() -> str:
    from .live_view import edge_key

    return edge_key()


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    with suppress(OSError):  # Windows: no Unix file modes (the folder's permissions protect it)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def write_edge_files(device: Device, token: str, *, org_name: str, site_name: str) -> Path:
    """cloud.json (same format as ``countvision-edge pair`` writes) and edge.yaml."""
    s = get_settings()
    edge_dir = s.edge_dir
    creds: dict[str, Any] = {
        "url": s.local_api_url.rstrip("/"),
        "device_id": str(device.id),
        "token": token,
        "organization": org_name,
        "site": site_name,
        "device_name": device.name,
        "edge_device_id": device.edge_device_id,
        "paired_at": time.time(),
    }
    _write_private(edge_dir / CREDENTIALS, json.dumps(creds, indent=2))
    _write_private(edge_dir / EDGE_CONFIG, edge_config_text(edge_dir))  # contains the live key
    return edge_dir


def status(db: Session) -> dict[str, Any]:
    """Public facts for the web app before login (no personal data)."""
    s = get_settings()
    return {"edition": s.edition, "version": __version__, "needs_setup": needs_setup(db),
            "email": s.email_backend != "off"}
