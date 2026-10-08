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
        return self

    @property
    def secure_cookies(self) -> bool:
        """Send cookies only over HTTPS when the site runs on HTTPS."""
        if self.cookie_secure is not None:
            return self.cookie_secure
        return self.web_url.startswith("https://")

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)


@lru_cache
def get_settings() -> Settings:
    """Settings singleton (tests clear the cache with get_settings.cache_clear())."""
    return Settings()
