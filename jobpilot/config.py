"""Process configuration from .env. Runtime knobs (threshold, cap, ...) live in the settings table."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
CONFIG_DIR = ROOT / "config"


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_rpm: int = 8
    # Comma-separated models to switch to when GEMINI_MODEL's daily quota runs out
    gemini_fallback_models: str = ""

    database_url: str = f"sqlite:///{(DATA_DIR / 'jobpilot.db').as_posix()}"
    # Defaults to config/master_resume.md in this project
    resume_path: str = ""
    # Defaults to config/application_profile.yaml and data/browser_profile in this project
    application_profile_path: str = ""
    browser_profile_dir: str = ""


@lru_cache
def get_config() -> Config:
    return Config()


def browser_profile_dir() -> Path:
    """The signed-in Chrome profile the apply agent drives."""
    config = get_config()
    return Path(config.browser_profile_dir) if config.browser_profile_dir else DATA_DIR / "browser_profile"


def application_profile_path() -> Path:
    """Contact details and screening answers (personal, git-ignored)."""
    config = get_config()
    return Path(config.application_profile_path) if config.application_profile_path else CONFIG_DIR / "application_profile.yaml"
