"""Settings from environment variables (prefix CV_), with safe local defaults.

Everything runs locally for 0 EUR. Secrets (SECRET_KEY, SMTP password, Google client secret)
come only from the environment / .env file, never from the code.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET = "dev-secret-change-me-dev-secret-change-me"


class Settings(BaseSettings):
    """All cloud settings. Environment variable = CV_ + field name in capitals."""

    model_config = SettingsConfigDict(env_prefix="CV_", env_file=".env", extra="ignore")

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

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

    # Email: "smtp" (Mailpit in development), "console" (log only), "memory" (tests)
    email_backend: Literal["smtp", "console", "memory"] = "console"
    email_from: str = "CountVision <no-reply@countvision.local>"
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_starttls: bool = False
    # "celery" = send in the background worker, "sync" = send inside the request (tests, no worker)
    email_delivery: Literal["celery", "sync"] = "celery"

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

    @field_validator("web_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_production(self) -> Settings:
        if self.environment == "production":
            if self.secret_key == DEV_SECRET or len(self.secret_key) < 32:
                raise ValueError("CV_SECRET_KEY must be set to a long random value in production")
            if not self.web_url.startswith("https://"):
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
    def billing_enabled(self) -> bool:
        """Plans, limits and the trial apply only when Stripe is set up."""
        return bool(self.stripe_secret_key)

    @property
    def stripe_test_mode(self) -> bool:
        return (self.stripe_secret_key or "").startswith(("sk_test_", "rk_test_"))

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    """Settings singleton (tests clear the cache with get_settings.cache_clear())."""
    return Settings()
