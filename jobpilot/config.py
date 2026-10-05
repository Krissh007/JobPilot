"""Process configuration from .env. Runtime knobs (threshold, cap, ...) live in the settings table."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    gemini_api_key: str = ""
    gemini_model: str = ""
    gemini_rpm: int = 8

    database_url: str = f"sqlite:///{(DATA_DIR / 'jobpilot.db').as_posix()}"
    legacy_agent_dir: Path = Path(r"E:\job_search_agent")
    legacy_python: str = "python"
    # Defaults to the legacy agent's config/master_resume.md
    resume_path: str = ""


@lru_cache
def get_config() -> Config:
    return Config()
