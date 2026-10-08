"""
JobPilotFiller: the legacy LinkedIn EasyApplyFiller (unchanged: dialog detection,
step navigation, stop-before-Submit), with only the per-field answer lookup
replaced so every value is traced to resume / answer bank / Gemini draft.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from jobpilot.agents.apply.answers import AnswerBank, Resolved
from jobpilot.db.models import FieldSource
from jobpilot.legacy import bridge


@dataclass
class FieldRecord:
    step: int
    name: str
    value: str
    source: FieldSource
    needs_review: bool


# Consent / terms / privacy questions are yours alone: never ticked, chosen or answered by the agent,
# whatever the answer bank says. A form with one can't be auto-submitted.
CONSENT = re.compile(r"consent|\bi (hereby )?agree\b|\bagree (to|with) (the |our |its )?(terms|privacy|policy|processing|collection|use of)|terms (and|&) conditions|terms of (use|service)|privacy (policy|notice)"
                     r"|(collect|store|process)\w*,? (and|&) (store|process)|processing of (my )?(personal )?data", re.IGNORECASE)


def is_consent(question: str) -> bool:
    return bool(CONSENT.search(question))


def easy_apply_module():
    return bridge.easy_apply()


def make_filler_class():
    EasyApplyFiller = easy_apply_module().EasyApplyFiller

    class JobPilotFiller(EasyApplyFiller):
        def __init__(self, page: Any, bank: AnswerBank, drafter: Callable[[str, list[str] | None], str | None] | None,
                     max_steps: int = 8) -> None:
            super().__init__(page, answers={}, max_steps=max_steps)
            self.bank, self.drafter = bank, drafter
            self.records: list[FieldRecord] = []
            self.consent: list[str] = []  # required consent boxes/questions left for you
            self._seen: set[str] = set()

        def _note_consent(self, question: str) -> None:
            if question and question not in self.consent:
                self.consent.append(question)

        def _resolve(self, label: str, options: list[str] | None = None) -> Resolved | None:
            hit = self.bank.lookup(label)
            if hit is not None:
                return hit
            self.bank.note_unknown(label)
            if self.drafter is not None:
                drafted = self.drafter(label, options)
                if drafted:
                    return Resolved(drafted, FieldSource.llm, needs_review=True)
            return None

        def _record(self, step: int, label: str, resolved: Resolved) -> None:
            key = f"{step}|{label}"
            if key not in self._seen:
                self._seen.add(key)
                self.records.append(FieldRecord(step, label, resolved.value, resolved.source, resolved.needs_review))

        def _fill_step(self, dialog: Any, result: Any) -> None:
            for fieldset in dialog.locator("fieldset").all():
                self._fill_radio_group(fieldset, result)
            self._check_boxes(dialog)
            for control in dialog.locator("input, select, textarea").all():
                kind = (control.get_attribute("type") or "").lower()
                if kind in ("radio", "checkbox", "hidden", "file", "submit", "button"):
                    continue
                if not control.is_visible() or not control.is_editable():
                    continue
                label = self._label_for(dialog, control)
                if not label:
                    continue
                current = self._current_value(control)
                if current:  # pre-filled by LinkedIn from your profile
                    self._record(result.steps, label, Resolved(current, FieldSource.resume))
                    continue
                if is_consent(label):
                    self._note_consent(label)
                    continue
                options = None
                if control.evaluate("e => e.tagName.toLowerCase()") == "select":
                    options = [self._clean(o.inner_text()) for o in control.locator("option").all()]
                    options = [o for o in options if o and not o.lower().startswith("select")]
                resolved = self._resolve(label, options)
                if resolved and self._set_value(control, resolved.value):
                    result.filled_fields.append(label)
                    self._record(result.steps, label, resolved)
                elif self._is_required(control, label) and label not in result.unknown_questions:
                    result.unknown_questions.append(label)

        def _fill_radio_group(self, fieldset: Any, result: Any) -> None:
            radios = fieldset.locator("input[type='radio']")
            if radios.count() == 0 or not fieldset.is_visible():
                return
            legend = fieldset.locator("legend")
            question = self._clean(legend.first.inner_text()) if legend.count() else ""
            if not question:
                return
            checked = [r for r in radios.all() if r.is_checked()]
            if checked:
                self._record(result.steps, question, Resolved(self._label_for(fieldset, checked[0]), FieldSource.resume))
                return
            if is_consent(question):
                self._note_consent(question)
                return
            options = {self._label_for(fieldset, r).lower(): r for r in radios.all()}
            resolved = self._resolve(question, [self._label_for(fieldset, r) for r in radios.all()])
            if resolved and resolved.value.lower() in options:
                self._choose(fieldset, options[resolved.value.lower()])
                result.filled_fields.append(question)
                self._record(result.steps, question, resolved)
            elif question not in result.unknown_questions:
                result.unknown_questions.append(question)

        def _check_boxes(self, dialog: Any) -> None:
            """Required checkboxes (usually data-consent) are never ticked: record them for you."""
            for box in dialog.locator("input[type='checkbox']").all():
                try:
                    if box.is_checked():
                        continue
                    label = self._label_for(dialog, box)
                    if "follow" in label.lower():
                        continue
                    fieldset = box.locator("xpath=ancestor::fieldset[1]")
                    legend = fieldset.locator("legend") if fieldset.count() else None
                    question = self._clean(legend.first.inner_text()) if legend is not None and legend.count() else label
                    required = self._is_required(box, question) or label.rstrip().endswith("*")
                    if required or is_consent(question):
                        self._note_consent(question)
                except Exception:
                    continue

    return JobPilotFiller
