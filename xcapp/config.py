"""Settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_url: str
    secret_key: str
    token_key: str
    base_url: str
    team_timezone: str
    secure_cookies: bool
    # Garmin Connect Developer Program (OAuth 2.0 PKCE, per Garmin's published spec)
    garmin_client_id: str
    garmin_client_secret: str
    garmin_authorize_url: str
    garmin_token_url: str
    garmin_api_base: str
    garmin_file_hosts: tuple[str, ...]
    garmin_webhook_secret: str
    # Claude API
    anthropic_api_key: str
    anthropic_model: str
    anthropic_base_url: str
    nightly_hour: int
    team_name: str
    team_logo_file: str  # path to the approved logo (png, jpg, svg or webp)
    as_of_date: str  # optional fixed "today" (YYYY-MM-DD) for demos and testing

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.team_timezone)

    @property
    def garmin_configured(self) -> bool:
        return bool(self.garmin_client_id and self.garmin_client_secret)

    @property
    def ai_configured(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def garmin_redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/garmin/callback"

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///./data/xc.db"),
            secret_key=os.getenv("SECRET_KEY", ""),
            token_key=os.getenv("TOKEN_KEY", ""),
            base_url=os.getenv("BASE_URL", "http://localhost:8000"),
            team_timezone=os.getenv("TEAM_TIMEZONE", "America/Los_Angeles"),
            secure_cookies=_bool("SECURE_COOKIES", True),
            garmin_client_id=os.getenv("GARMIN_CLIENT_ID", ""),
            garmin_client_secret=os.getenv("GARMIN_CLIENT_SECRET", ""),
            garmin_authorize_url=os.getenv("GARMIN_AUTHORIZE_URL", "https://connect.garmin.com/oauth2Confirm"),
            garmin_token_url=os.getenv("GARMIN_TOKEN_URL", "https://diauth.garmin.com/di-oauth2-service/oauth/token"),
            garmin_api_base=os.getenv("GARMIN_API_BASE", "https://apis.garmin.com"),
            garmin_file_hosts=tuple(
                h.strip().lower() for h in os.getenv("GARMIN_FILE_HOSTS", "apis.garmin.com").split(",") if h.strip()
            ),
            garmin_webhook_secret=os.getenv("GARMIN_WEBHOOK_SECRET", ""),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
            anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5"),
            anthropic_base_url=os.getenv("ANTHROPIC_BASE_URL", ""),
            nightly_hour=int(os.getenv("NIGHTLY_HOUR", "4")),
            team_name=os.getenv("TEAM_NAME", "XC Training"),
            team_logo_file=os.getenv("TEAM_LOGO_FILE", ""),
            as_of_date=os.getenv("AS_OF_DATE", ""),
        )

    def problems(self) -> list[str]:
        """Configuration problems that should stop a production start."""
        out = []
        if len(self.secret_key) < 32:
            out.append("SECRET_KEY must be at least 32 characters (run: python -m xcapp.cli gen-keys).")
        if not self.token_key:
            out.append("TOKEN_KEY is missing (run: python -m xcapp.cli gen-keys).")
        try:
            ZoneInfo(self.team_timezone)
        except Exception:
            out.append(f"TEAM_TIMEZONE '{self.team_timezone}' is not a valid time zone.")
        if self.as_of_date:
            try:
                import datetime as _dt
                _dt.date.fromisoformat(self.as_of_date)
            except ValueError:
                out.append("AS_OF_DATE must look like 2026-10-25.")
        if self.garmin_client_id and not self.garmin_webhook_secret:
            out.append("GARMIN_WEBHOOK_SECRET is required once Garmin is configured.")
        return out


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def reload_settings() -> Settings:
    global _settings
    _settings = Settings.from_env()
    return _settings
