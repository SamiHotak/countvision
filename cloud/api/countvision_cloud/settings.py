"""Settings from environment variables (prefix CV_), with safe local defaults.

Everything runs locally for 0 EUR. Secrets (SECRET_KEY, SMTP password, Google client secret)
come only from the environment / .env file, never from the code.
"""

from __future__ import annotations

import contextlib
import logging
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-secret-change-me-dev-secret-change-me"
SECRET_FILE = "secret_key"  # local edition: a random key created once in data_dir

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    """All cloud settings. Environment variable = CV_ + field name in capitals."""

    model_config = SettingsConfigDict(env_prefix="CV_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # "cloud" = the SaaS (PostgreSQL + Redis + Celery workers, Stripe, Google login).
    # "local" = CountVision Local on ONE Windows PC without internet: no Redis and no Celery (an
    # in-process store and timers replace them), a licence key instead of Stripe, a first-start
    # wizard, the built-in edge agent paired automatically. See countvision_cloud/local/.
    edition: Literal["cloud", "local"] = "cloud"

    # Public URL of the web app (Next.js). Links in emails and the Google redirect use it.
    web_url: str = "http://localhost:3000"
    secret_key: str = DEV_SECRET

    database_url: str = "postgresql+psycopg://countvision:countvision@localhost:5433/countvision"
    redis_url: str = "redis://localhost:6379/0"

    # Sessions (cookie login)
    session_cookie: str = "cv_session"
    session_days: int = Field(default=30, ge=1, le=365)
    cookie_secure: bool | None = None  # None = secure cookies when web_url is https

    # Tokens sent by email
    verify_email_hours: int = 48
    reset_password_minutes: int = 60
    invite_days: int = 7

    # Login protection: max failed logins per email and per IP in the window
    login_max_attempts: int = 10
    login_window_s: int = 15 * 60
    # New accounts per IP and hour (raise it for browser tests that sign up many times)
    signups_per_ip_hour: int = 20

    # Email: "smtp" (Mailpit in development), "console" (log only), "memory" (tests), "off" (dropped:
    # the local edition without an SMTP server)
    email_backend: Literal["smtp", "console", "memory", "off"] = "console"
    email_from: str = "CountVision <no-reply@countvision.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    # "celery" = send in the background worker, "sync" = send inside the request (tests, no worker),
    # "thread" = a background thread of the API process (local edition). Also used for Telegram and
    # webhook messages.
    email_delivery: Literal["celery", "sync", "thread"] = "celery"

    # Files of video analysis jobs (uploads, annotated videos, reports). Docker: a volume.
    data_dir: str = "data"

    # Alerts and webhooks (Phase 4 B)
    telegram_api_url: str = "https://api.telegram.org"  # tests point this at a fake server
    # Webhooks to private addresses (localhost, 192.168.x.x ...). None = allowed except in
    # production (protects the server's own network from requests made on a customer's behalf).
    webhook_allow_private: bool | None = None
    webhook_timeout_s: float = 10.0

    # Video analysis jobs. "celery" = the video worker container (queue "video"); "thread" = inside
    # the API process (development without Docker, browser tests); "off" = uploads only.
    video_runner: Literal["celery", "thread", "off"] = "celery"
    video_detector: Literal["onnx", "openvino", "blobs"] = "onnx"
    video_model: str = "models/yolox_tiny.onnx"  # Apache-2.0 YOLOX-Tiny; downloaded when missing
    video_device: str = "auto"  # auto | cpu | cuda (needs onnxruntime-gpu on a GPU server)
    video_max_bytes: int = 2 * 1024**3
    video_max_minutes: int = 120
    video_keep_days: int = Field(default=7, ge=1, le=90)  # annotated videos are deleted after this
    video_jobs_per_org: int = 3  # waiting or running at the same time

    # Public API: requests per API key and minute
    api_rate_per_minute: int = 120

    # "Add sample data" button (a Demo café with 5 weeks of invented numbers, clearly labelled)
    sample_data_enabled: bool = True

    # Billing with Stripe (Phase 4 C). OFF when no secret key is set: then every organization
    # has no limits (self-hosted / development). TEST MODE ONLY until you decide to go live:
    # a live key (sk_live_...) is refused unless CV_STRIPE_ALLOW_LIVE=true.
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None  # whsec_... from "stripe listen" or the Stripe dashboard
    stripe_api_url: str = "https://api.stripe.com"  # tests point this at a fake Stripe
    stripe_allow_live: bool = False
    trial_days: int = Field(default=14, ge=0, le=90)

    # Google login: off when the client id is empty
    google_client_id: str | None = None
    google_client_secret: str | None = None

    # --- local edition only (edition = "local") ---------------------------------------------------
    # Data folder of the built-in edge agent: the API writes cloud.json (its device token) and
    # edge.yaml there. Default: <data_dir>/edge
    local_edge_dir: str | None = None
    # How the built-in edge agent reaches this API (same PC)
    local_api_url: str = "http://127.0.0.1:8001"
    # Port of the built-in edge agent's live camera picture (127.0.0.1 only; key protected)
    local_live_port: int = Field(default=8002, ge=1, le=65535)
    # In-process timers (alerts, reports, cleanup ...). Tests switch them off and call the tasks.
    local_background: bool = True
    # Folder with pg_dump / pg_restore (the bundled PostgreSQL). Empty = search the PATH.
    pg_bin_dir: str | None = None
    # Where "Create backup" writes. Default: <data_dir>/backups
    backup_dir: str | None = None
    # Licence public key override (base64). Only honoured in development/test: in production the
    # built-in key is used, so nobody can sign their own licences by setting a variable.
    licence_public_key: str | None = None

    @field_validator("web_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _local_defaults(self) -> Settings:
        """The local edition has no Celery worker and usually no mail server."""
        if self.edition != "local":
            return self
        given = self.model_fields_set
        if "email_delivery" not in given:
            self.email_delivery = "thread"
        if "video_runner" not in given:
            self.video_runner = "thread"
        if "email_backend" not in given:
            self.email_backend = "smtp" if "smtp_host" in given else "off"
        if self.email_delivery == "celery":
            raise ValueError("The local edition has no Celery worker: use CV_EMAIL_DELIVERY=thread or sync")
        if self.video_runner == "celery":
            raise ValueError("The local edition has no Celery worker: use CV_VIDEO_RUNNER=thread")
        if self.secret_key == DEV_SECRET and self.environment != "test":
            self.secret_key = _local_secret(Path(self.data_dir))
        return self

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.environment == "production":
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                raise ValueError("CV_SECRET_KEY must be set to a long random value in production")
            # The local edition runs on http://localhost (and the shop network) without a certificate.
            if self.edition == "cloud" and not self.web_url.startswith("https://"):
                raise ValueError("CV_WEB_URL must be https in production")
        key = self.stripe_secret_key or ""
        if key and not key.startswith(("sk_test_", "rk_test_")) and not self.stripe_allow_live:
            raise ValueError(
                "CV_STRIPE_SECRET_KEY is not a TEST key (sk_test_...). Live keys charge real money; "
                "set CV_STRIPE_ALLOW_LIVE=true only when you really go live."
            )
        return self

    @property
    def secure_cookies(self) -> bool:
        """Send cookies only over HTTPS when the site runs on HTTPS."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.web_url.startswith("https://")

    @property
    def private_webhooks_allowed(self) -> bool:
        if self.webhook_allow_private is not None:
            return self.webhook_allow_private
        return self.environment != "production"

    @property
    def is_local(self) -> bool:
        return self.edition == "local"

    @property
    def billing_enabled(self) -> bool:
        """Stripe plans, limits and the trial apply only when Stripe is set up (never in the local
        edition: there a licence key sets the limits, see local/licence.py)."""
        return bool(self.stripe_secret_key) and not self.is_local

    @property
    def stripe_test_mode(self) -> bool:
        return (self.stripe_secret_key or "").startswith(("sk_test_", "rk_test_"))

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret) and not self.is_local

    @property
    def edge_dir(self) -> Path:
        return Path(self.local_edge_dir) if self.local_edge_dir else Path(self.data_dir) / "edge"

    @property
    def backups_path(self) -> Path:
        return Path(self.backup_dir) if self.backup_dir else Path(self.data_dir) / "backups"


def _local_secret(data_dir: Path) -> str:
    """Read (or create once) the random secret of this installation. It encrypts camera passwords
    and webhook secrets, and signs login state: keep it with the data (backups contain it too)."""
    path = data_dir / SECRET_FILE
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) >= 32:
            return value
    except FileNotFoundError:
        pass
    data_dir.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(48)
    path.write_text(value + "\n", encoding="utf-8")
    with contextlib.suppress(OSError):  # Windows: the folder's permissions protect it
        path.chmod(0o600)
    log.info("Created a new secret key in %s", path)
    return value


@lru_cache
def get_settings() -> Settings:
    """Settings singleton (tests clear the cache with get_settings.cache_clear())."""
    return Settings()
