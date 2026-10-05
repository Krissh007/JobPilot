"""The master resume every agent scores and tailors against."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from jobpilot.config import get_config


class ResumeError(RuntimeError):
    pass


@dataclass(frozen=True)
class Resume:
    path: Path
    text: str

    @property
    def version(self) -> str:
        # Content hash: editing the resume changes the version and invalidates cached scores.
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


def resume_path() -> Path:
    config = get_config()
    return Path(config.resume_path) if config.resume_path else Path(config.legacy_agent_dir) / "config" / "master_resume.md"


def load_resume(path: Path | None = None) -> Resume:
    path = path or resume_path()
    if not path.is_file():
        raise ResumeError(f"Master resume not found: {path} (set RESUME_PATH in .env)")
    if path.suffix.lower() not in (".md", ".txt"):
        raise ResumeError(f"{path.name}: only .md/.txt resumes are supported for now (.docx comes with the docs agent)")
    text = path.read_text(encoding="utf-8").strip()
    if len(text) < 200:
        raise ResumeError(f"{path} looks empty ({len(text)} characters)")
    return Resume(path=path, text=text)
