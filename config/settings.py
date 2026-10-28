"""Application configuration and settings models.

Implements: FR-001 (data directory), FR-003 (configuration model),
            NFR-QW1 (keyring integration).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, model_validator

_logger = logging.getLogger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

# ---------------------------------------------------------------------------
# Keyring helpers — lazy-checked, graceful fallback to plaintext
# ---------------------------------------------------------------------------

_keyring_available: bool | None = None  # lazy-init sentinel
KEYRING_SERVICE = "autoapply"
KEYRING_KEY_NAME = "llm_api_key"
LLM_PROVIDER_ENV_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
    "ollama": ("OLLAMA_API_KEY",),
}


def _check_keyring() -> bool:
    """Return True if the OS keyring backend is usable. Result is cached."""
    global _keyring_available
    if _keyring_available is not None:
        return _keyring_available
    try:
        import keyring
        keyring.get_password(KEYRING_SERVICE, "__probe__")
        _keyring_available = True
    except Exception:
        _logger.warning("keyring unavailable — API key stored in plaintext config")
        _keyring_available = False
    return _keyring_available


class UserProfile(BaseModel):
    first_name: str
    last_name: str
    email: str
    phone_country_code: str = "+1"
    phone: str
    address_line1: str = ""
    address_line2: str = ""
    city: str
    state: str
    zip_code: str = ""
    country: str = "United States"
    bio: str
    linkedin_url: str | None = None
    portfolio_url: str | None = None
    fallback_resume_path: str | None = None
    screening_answers: dict = {}

    # Backward-compatible property — used by AI engine prompts and Lever/Indeed
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    # Backward-compatible property — formatted location string
    @property
    def location(self) -> str:
        parts = [self.city, self.state]
        loc = ", ".join(p for p in parts if p)
        if self.country and self.country != "United States":
            loc = f"{loc}, {self.country}" if loc else self.country
        return loc

    # Full phone with country code
    @property
    def phone_full(self) -> str:
        if self.phone_country_code and not self.phone.startswith("+"):
            return f"{self.phone_country_code}{self.phone}"
        return self.phone

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data):
        """Accept old config format with full_name and location strings."""
        if isinstance(data, dict):
            # Migrate full_name → first_name + last_name
            if "full_name" in data and "first_name" not in data:
                parts = data.pop("full_name", "").split(None, 1)
                data["first_name"] = parts[0] if parts else ""
                data["last_name"] = parts[1] if len(parts) > 1 else ""
            # Migrate location → city + state
            if "location" in data and "city" not in data:
                loc = data.pop("location", "")
                parts = [p.strip() for p in loc.split(",")]
                data["city"] = parts[0] if parts else ""
                data["state"] = parts[1] if len(parts) > 1 else ""
                if len(parts) > 2:
                    data["country"] = parts[2]
        return data


class SearchCriteria(BaseModel):
    job_titles: list[str]
    locations: list[str]
    remote_only: bool = False
    salary_min: int | None = None
    keywords_include: list[str] = []
    keywords_exclude: list[str] = []
    experience_levels: list[str] = ["mid", "senior"]


class ScheduleConfig(BaseModel):
    enabled: bool = False
    days_of_week: list[str] = ["mon", "tue", "wed", "thu", "fri"]
    start_time: str = "09:00"  # HH:MM in 24-hour local time
    end_time: str = "17:00"    # HH:MM in 24-hour local time


class LLMConfig(BaseModel):
    provider: str = ""  # "anthropic" | "openai" | "google" | "deepseek" | "ollama"
    api_key: str = ""
    model: str = ""  # Empty = use default for provider


class ResumeReuseConfig(BaseModel):
    """Configuration for smart resume reuse via Knowledge Base assembly."""
    enabled: bool = True
    min_score: float = 0.0
    min_experience_bullets: int = 6
    scoring_method: str = "auto"  # "tfidf" | "onnx" | "auto"
    cover_letter_strategy: str = "generate"  # "generate" | "template"


class LatexConfig(BaseModel):
    """Configuration for LaTeX resume compilation."""
    template: str = "classic"  # template name in templates/latex/
    font_family: str = "helvetica"  # helvetica, times, palatino
    font_size: int = 11  # 10, 11, 12
    margin: str = "0.75in"


class EmailConfig(BaseModel):
    """Employer email settings; the password stays in the environment or OS keyring."""
    enabled: bool = False
    provider: str = "gmail"  # "gmail" | "outlook" | "yahoo" | "icloud" | "custom"
    imap_host: str = ""  # only used when provider == "custom"
    imap_port: int = 993
    username: str = ""
    folder: str = "INBOX"
    poll_interval_minutes: int = 15
    lookback_days: int = 14
    auto_update_status: bool = True
    min_confidence: float = 0.8


class BotConfig(BaseModel):
    enabled_platforms: list[str] = ["linkedin", "indeed"]
    min_match_score: int = 75
    max_applications_per_day: int = 50
    delay_between_applications_seconds: int = 45
    search_interval_seconds: int = 1800
    apply_mode: str = "full_auto"  # "full_auto" | "review" | "watch"
    watch_mode: bool = False  # Deprecated: use apply_mode instead
    cover_letter_enabled: bool = True
    cover_letter_template: str = ""
    schedule: ScheduleConfig = ScheduleConfig()


class AppConfig(BaseModel):
    profile: UserProfile
    search_criteria: SearchCriteria
    bot: BotConfig = BotConfig()
    llm: LLMConfig = LLMConfig()
    resume_reuse: ResumeReuseConfig = ResumeReuseConfig()
    latex: LatexConfig = LatexConfig()
    email: EmailConfig = EmailConfig()
    company_blacklist: list[str] = []
    version: str = "2.0"


def get_data_dir() -> Path:
    return Path.home() / ".autoapply"


def get_database_path() -> Path:
    """Return the SQLite database path, optionally overridden by the environment."""
    configured_path = os.environ.get("AUTOAPPLY_DATABASE_PATH", "").strip()
    if configured_path:
        return Path(configured_path).expanduser().resolve()
    return get_data_dir() / "autoapply.db"


def _environment_llm_api_key(provider: str) -> str:
    provider_key_names = LLM_PROVIDER_ENV_KEYS.get(provider, ())
    for key_name in provider_key_names:
        value = os.environ.get(key_name, "").strip()
        if value:
            return value
    return os.environ.get("AUTOAPPLY_LLM_API_KEY", "").strip()


def _apply_environment_config(config: AppConfig) -> None:
    """Apply optional environment overrides without persisting their secrets."""
    provider = os.environ.get("AUTOAPPLY_LLM_PROVIDER", "").strip().lower()
    configured_provider_keys = {
        name: next(
            os.environ[key_name].strip()
            for key_name in key_names
            if os.environ.get(key_name, "").strip()
        )
        for name, key_names in LLM_PROVIDER_ENV_KEYS.items()
        if any(os.environ.get(key_name, "").strip() for key_name in key_names)
    }

    if not provider:
        if config.llm.provider in configured_provider_keys:
            provider = config.llm.provider
        elif len(configured_provider_keys) == 1:
            provider = next(iter(configured_provider_keys))
        elif len(configured_provider_keys) > 1:
            raise ValueError(
                "Multiple provider API keys are set. Set AUTOAPPLY_LLM_PROVIDER "
                "to anthropic, openai, google, deepseek, or ollama."
            )
        else:
            provider = config.llm.provider

    if provider and provider not in LLM_PROVIDER_ENV_KEYS:
        raise ValueError(
            "AUTOAPPLY_LLM_PROVIDER must be anthropic, openai, google, deepseek, or ollama."
        )

    if provider:
        config.llm.provider = provider
        environment_key = _environment_llm_api_key(provider)
        if environment_key:
            config.llm.api_key = environment_key
        elif os.environ.get("AUTOAPPLY_LLM_PROVIDER", "").strip():
            config.llm.api_key = ""
    elif os.environ.get("AUTOAPPLY_LLM_API_KEY", "").strip():
        raise ValueError(
            "Set AUTOAPPLY_LLM_PROVIDER when using AUTOAPPLY_LLM_API_KEY."
        )

    model = os.environ.get("AUTOAPPLY_LLM_MODEL", "").strip()
    if model:
        config.llm.model = model

    email_updates: dict = {}
    for field, env_name in (
        ("provider", "AUTOAPPLY_EMAIL_PROVIDER"),
        ("imap_host", "AUTOAPPLY_EMAIL_IMAP_HOST"),
        ("username", "AUTOAPPLY_EMAIL_USERNAME"),
        ("folder", "AUTOAPPLY_EMAIL_FOLDER"),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            email_updates[field] = value
    if "provider" in email_updates and email_updates["provider"] not in {
        "gmail", "outlook", "yahoo", "icloud", "custom"
    }:
        raise ValueError(
            "AUTOAPPLY_EMAIL_PROVIDER must be gmail, outlook, yahoo, icloud, or custom."
        )

    email_port = os.environ.get("AUTOAPPLY_EMAIL_IMAP_PORT", "").strip()
    if email_port:
        try:
            parsed_port = int(email_port)
        except ValueError as exc:
            raise ValueError("AUTOAPPLY_EMAIL_IMAP_PORT must be an integer.") from exc
        if not 1 <= parsed_port <= 65535:
            raise ValueError("AUTOAPPLY_EMAIL_IMAP_PORT must be between 1 and 65535.")
        email_updates["imap_port"] = parsed_port

    email_enabled = os.environ.get("AUTOAPPLY_EMAIL_ENABLED", "").strip().lower()
    if email_enabled:
        if email_enabled not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise ValueError(
                "AUTOAPPLY_EMAIL_ENABLED must be a boolean value such as true or false."
            )
        email_updates["enabled"] = email_enabled in {"1", "true", "yes", "on"}

    if email_updates:
        config.email = config.email.model_copy(update=email_updates)
    if config.email.enabled:
        if not config.email.username:
            raise ValueError(
                "An email IMAP username must be configured when monitoring is enabled."
            )
        if config.email.provider == "custom" and not config.email.imap_host:
            raise ValueError(
                "An IMAP host must be configured for the custom email provider."
            )
        if not get_email_password():
            raise ValueError(
                "An email IMAP password must be set in the environment or OS keyring "
                "when monitoring is enabled."
            )


def load_config() -> AppConfig | None:
    config_path = get_data_dir() / "config.json"
    if not config_path.exists():
        return None
    with open(config_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    config = AppConfig(**data)

    # Retrieve API key from keyring if available
    if _check_keyring():
        import keyring

        stored_key = keyring.get_password(KEYRING_SERVICE, KEYRING_KEY_NAME)
        if stored_key:
            config.llm.api_key = stored_key
        elif config.llm.api_key:
            # Migration: move plaintext key into keyring
            keyring.set_password(
                KEYRING_SERVICE, KEYRING_KEY_NAME, config.llm.api_key
            )
            _save_config_raw(config, strip_api_key=True)
            _logger.info("Migrated API key from config.json to OS keyring")

    _apply_environment_config(config)
    return config


def save_config(config: AppConfig) -> None:
    environment_key = _environment_llm_api_key(config.llm.provider)
    # Store API key in keyring if possible
    if config.llm.api_key and not environment_key and _check_keyring():
        import keyring

        keyring.set_password(
            KEYRING_SERVICE, KEYRING_KEY_NAME, config.llm.api_key
        )

    _save_config_raw(
        config,
        strip_api_key=_check_keyring() or bool(environment_key),
    )


def _save_config_raw(config: AppConfig, strip_api_key: bool = False) -> None:
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    config_path = data_dir / "config.json"
    dump = config.model_dump()
    if strip_api_key:
        dump.get("llm", {})["api_key"] = ""
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(dump, f, indent=2)


# ---------------------------------------------------------------------------
# Email (IMAP) password — environment first, then keyring / 0600 file fallback.
# Never written to config.json and never returned by any API.
# ---------------------------------------------------------------------------

EMAIL_KEYRING_KEY_NAME = "email_imap_password"


def _email_secret_path() -> Path:
    return get_data_dir() / ".email_secret"


def get_email_password() -> str:
    """Return the environment or stored IMAP password, or "" if none is set."""
    environment_password = os.environ.get("AUTOAPPLY_EMAIL_IMAP_PASSWORD", "")
    if environment_password:
        return environment_password
    if _check_keyring():
        import keyring

        try:
            return keyring.get_password(KEYRING_SERVICE, EMAIL_KEYRING_KEY_NAME) or ""
        except Exception:
            _logger.warning("keyring read failed for email password")
    path = _email_secret_path()
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def set_email_password(password: str) -> None:
    """Store the IMAP password (empty string deletes it)."""
    if not password:
        delete_email_password()
        return
    if _check_keyring():
        import keyring

        try:
            keyring.set_password(KEYRING_SERVICE, EMAIL_KEYRING_KEY_NAME, password)
            _email_secret_path().unlink(missing_ok=True)
            return
        except Exception:
            _logger.warning("keyring write failed — falling back to 0600 file")
    path = _email_secret_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(password, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass  # Windows


def delete_email_password() -> None:
    if _check_keyring():
        import keyring

        try:
            keyring.delete_password(KEYRING_SERVICE, EMAIL_KEYRING_KEY_NAME)
        except Exception:
            pass  # not set / backend error — nothing to delete
    _email_secret_path().unlink(missing_ok=True)


def has_email_password() -> bool:
    return bool(get_email_password())


def is_first_run() -> bool:
    return not (get_data_dir() / "config.json").exists()
