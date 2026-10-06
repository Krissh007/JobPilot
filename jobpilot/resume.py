"""The master resume every agent scores and tailors against (RESUME_PATH in .env)."""

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
        # Content hash: editing the resume changes the version and invalidates cached LLM results.
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:12]


def resume_path() -> Path:
    config = get_config()
    return Path(config.resume_path) if config.resume_path else Path(config.legacy_agent_dir) / "config" / "master_resume.md"


def load_resume(path: Path | None = None) -> Resume:
    path = path or resume_path()
    if not path.is_file():
        raise ResumeError(f"Master resume not found: {path} (set RESUME_PATH in .env)")
    suffix = path.suffix.lower()
    if suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8").strip()
    elif suffix in (".docx", ".pdf"):
        from jobpilot.legacy.bridge import load_master_resume

        try:
            text = load_master_resume(path)  # same reader the tailoring uses, so both see identical text
        except ValueError as error:
            raise ResumeError(str(error)) from error
    else:
        raise ResumeError(f"{path.name}: use a .docx, .pdf, .md or .txt resume")
    if len(text) < 200:
        raise ResumeError(f"{path} looks empty ({len(text)} characters)")
    return Resume(path=path, text=text)
