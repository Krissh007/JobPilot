"""Phase 3: the tailoring, apply and resume code lives inside JobPilot; nothing needs the old project."""

from pathlib import Path

from jobpilot import config as cfg

ROOT = Path(__file__).resolve().parents[1]
OLD_PROJECT = "job_search" + "_agent"  # split so this file does not match itself


def test_no_source_file_references_the_old_project():
    hits = [str(p.relative_to(ROOT)) for folder in ("jobpilot", "tests", "scripts")
            for p in (ROOT / folder).rglob("*") if p.suffix in {".py", ".ps1"} and OLD_PROJECT in p.read_text(encoding="utf-8")]
    assert hits == []


def test_apply_and_tailoring_modules_live_in_the_tree():
    from jobpilot.agents.apply import easy_apply, naukri_applier
    from jobpilot.tailoring import generator, tailor

    assert hasattr(naukri_applier, "NaukriApplier") and hasattr(easy_apply, "EasyApplyFiller")
    assert tailor.__name__.startswith("jobpilot.tailoring") and generator.__name__.startswith("jobpilot.tailoring")


def test_retired_pipeline_is_gone():
    assert not (ROOT / "jobpilot" / "legacy").exists()
    for name in ("search", "score", "docs"):
        assert not (ROOT / "jobpilot" / "agents" / f"{name}.py").exists()


def test_default_paths_are_inside_the_project(monkeypatch):
    monkeypatch.setattr(cfg, "get_config", lambda: cfg.Config(_env_file=None, resume_path="", application_profile_path="", browser_profile_dir=""))
    assert cfg.browser_profile_dir() == ROOT / "data" / "browser_profile"
    assert cfg.application_profile_path() == ROOT / "config" / "application_profile.yaml"
