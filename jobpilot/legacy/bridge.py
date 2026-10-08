"""
Import the legacy agent's pure modules (tailoring, resume reader) into JobPilot
without copying or editing them. Only modules with no browser or database
side effects are imported this way; discovery runs in its own process
(legacy_shim.py).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

from jobpilot.config import get_config


def legacy_root() -> Path:
    return Path(get_config().legacy_agent_dir)


def _ensure_path() -> None:
    root = str(legacy_root())
    if root not in sys.path:
        sys.path.append(root)  # appended, so JobPilot's own modules always win


def tailoring() -> tuple[ModuleType, ModuleType, ModuleType]:
    """(tailor, docx_writer, generator) from E:\\job_search_agent\\src\\tailoring."""
    _ensure_path()
    from src.tailoring import docx_writer, generator, tailor

    return tailor, docx_writer, generator


def easy_apply() -> ModuleType:
    """The legacy LinkedIn Easy Apply filler (needs only Playwright)."""
    _ensure_path()
    from src.apply import easy_apply as module

    return module


def load_master_resume(path: Path) -> str:
    """Plain text of a .docx/.pdf/.md/.txt resume, read exactly as the legacy tailoring reads it."""
    _ensure_path()
    from src.tailoring.master_resume import load_master_resume as load

    return load(path)


def application_profile_path() -> Path:
    return legacy_root() / "config" / "application_profile.yaml"
