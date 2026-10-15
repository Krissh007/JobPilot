from __future__ import annotations

from jobpilot.tailoring import checks
from tests.doc_fixtures import MASTER
from jobpilot.tailoring.docx_writer import fit_keywords


def test_master_dates_lookup_handles_company_variants():
    assert checks.master_dates_for("LogNormal Analytics Pvt. Ltd.", MASTER) == ["Oct 2025 – Sep 2026"]
    assert checks.master_dates_for("Softic Digitech", MASTER) == ["Feb 2024 – Sep 2025"]
    assert checks.master_dates_for("Unknown Corp", MASTER) == []


def test_word_properties_fit_255_chars():
    
    long = ["Business Process Analysis", "Cross-functional Collaboration", "Stakeholder Management"] * 10
    kept = fit_keywords(long)
    assert 0 < len(kept) < 25 and len(", ".join(kept)) <= 255 and kept == long[:len(kept)]
    assert fit_keywords(["SQL", "Jira"]) == ["SQL", "Jira"]
