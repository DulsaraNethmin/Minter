"""Environment-driven configuration."""

import logging

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    host: str = "0.0.0.0"  # noqa: S104 - container-local, no ports published
    port: int = 8191
    log_level: str = "INFO"

    # Solver attempts before giving up. Byparr defaults this to sys.maxsize and lets
    # the timeout kill it; we would rather fail fast and let the caller degrade.
    max_attempts: int = 5

    # Seconds allowed for a single mint, end to end.
    default_timeout: int = 60

    # BCP-47 tag, or "auto" to derive from the exit IP. Locale that disagrees with
    # the exit IP is itself a detection signal, so "auto" is the safe default.
    browser_locale: str = "auto"

    # Images/fonts/video are never needed to solve a challenge and only slow the mint.
    block_media: bool = True

    proxy_server: str | None = None
    proxy_username: str | None = None
    proxy_password: str | None = None


settings = Settings()

LOG_LEVEL = logging.getLevelNamesMapping()[settings.log_level.upper()]

HOST = settings.host
PORT = settings.port
MAX_ATTEMPTS = settings.max_attempts
DEFAULT_TIMEOUT = settings.default_timeout
BROWSER_LOCALE = settings.browser_locale
BLOCK_MEDIA = settings.block_media
PROXY_SERVER = settings.proxy_server
PROXY_USERNAME = settings.proxy_username
PROXY_PASSWORD = settings.proxy_password


def proxy_config() -> dict[str, str] | None:
    """Playwright-shaped proxy dict, or None when no proxy is configured."""
    if not PROXY_SERVER:
        return None
    cfg = {"server": PROXY_SERVER}
    if PROXY_USERNAME:
        cfg["username"] = PROXY_USERNAME
    if PROXY_PASSWORD:
        cfg["password"] = PROXY_PASSWORD
    return cfg


def configure_logging() -> logging.Logger:
    """Attach to uvicorn's logger so output is not doubled."""
    logger = logging.getLogger("uvicorn.error")
    logger.setLevel(LOG_LEVEL)
    if not logger.handlers:
        logger.addHandler(logging.StreamHandler())
    return logger
