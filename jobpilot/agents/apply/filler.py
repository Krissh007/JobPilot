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


# A saved draft turns LinkedIn's "Easy Apply" button into "Continue", with "You last modified this application ..."
# beside it. "Continue" alone is NOT an Easy Apply button, so it only counts together with that sentence.
CONTINUE_BUTTON = re.compile(r"^\s*continue\s*$", re.IGNORECASE)
DRAFT_NOTICE = re.compile(r"you last modified this application", re.IGNORECASE)


# Finds required controls still empty on the current step and names each one. LinkedIn does not always raise an error
# element with a label inside it, which left the old lookup with nothing to report ("a required field LinkedIn didn't name").
# Real forms (16 Oct 2026, Aptita #592) also mark a Yes/No radio group as required only by a "*" at the end of the question
# text in the wrapper around <fieldset role=radiogroup>: the inputs carry no required / aria-required at all.
UNANSWERED_REQUIRED_JS = r"""dialog => {
  const first = t => String(t || '').split('\n').map(s => s.trim()).filter(Boolean)[0] || '';
  const visible = e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length);
  const byIds = ids => String(ids || '').split(/\s+/).map(i => { const n = document.getElementById(i); return n ? n.innerText : ''; }).join(' ');
  const nameOf = el => {
    const group = el.type === 'radio' || el.type === 'checkbox';
    const legend = el.closest('fieldset') ? el.closest('fieldset').querySelector('legend') : null;
    if (group && legend && first(legend.innerText)) return first(legend.innerText);
    if (el.type === 'radio') {  // a radio's own <label> is an option ("Yes"); the question is its aria-label or the text around the group
      if (first(el.getAttribute('aria-label'))) return first(el.getAttribute('aria-label'));
      const set = el.closest('fieldset'); const top = set && set.parentElement ? first(set.parentElement.innerText).replace(/\s*\*\s*$/, '') : '';
      if (top && top.length < 200) return top;
    }
    if (el.labels && el.labels.length && first(el.labels[0].innerText)) return first(el.labels[0].innerText);
    if (first(el.getAttribute('aria-label'))) return first(el.getAttribute('aria-label'));
    if (first(byIds(el.getAttribute('aria-labelledby')))) return first(byIds(el.getAttribute('aria-labelledby')));
    if (legend && first(legend.innerText)) return first(legend.innerText);
    const wrap = el.closest('[data-test-form-element], .jobs-easy-apply-form-element, .fb-dash-form-element, .artdeco-text-input--container');
    const near = wrap ? wrap.querySelector('label, legend, .fb-dash-form-element__label') : null;
    if (near && first(near.innerText)) return first(near.innerText);
    if (first(el.getAttribute('placeholder'))) return first(el.getAttribute('placeholder'));
    let own = '';
    if (el.parentElement) { const c = el.parentElement.cloneNode(true); c.querySelectorAll('select, input, textarea, button').forEach(n => n.remove()); own = first(c.textContent); }
    if (own && own.length < 140) return own;
    if (el.tagName === 'SELECT') {
      const opts = [...el.options].map(o => first(o.innerText)).filter(o => o && !/^select/i.test(o)).slice(0, 4);
      return 'an unlabelled dropdown' + (opts.length ? ' (' + opts.join(' / ') + ')' : '');
    }
    return 'an unlabelled ' + (el.tagName === 'TEXTAREA' ? 'text box' : (el.type || 'field') + ' field');
  };
  const groupMarkedRequired = el => {
    if (el.type !== 'radio') return false;
    const set = el.closest('fieldset');
    const wrap = set && set.parentElement ? set.parentElement : null;
    const text = wrap ? String(wrap.innerText || '') : '';
    return /\*\s*$/.test(first(text)) || /\*\s*$/.test(first(set ? set.innerText : '')) || /this field is required/i.test(text);
  };
  const empty = el => {
    if (el.type === 'checkbox') return !el.checked;
    if (el.type === 'radio') return ![...dialog.querySelectorAll('input[type=radio]')].filter(r => r.name === el.name).some(r => r.checked);
    if (el.tagName === 'SELECT') return !el.value || /^select/i.test(first(el.options[el.selectedIndex] ? el.options[el.selectedIndex].innerText : ''));
    return !String(el.value || '').trim();
  };
  const out = [];
  for (const el of dialog.querySelectorAll('input, select, textarea')) {
    if (['hidden', 'file', 'submit', 'button'].includes(el.type) || !visible(el)) continue;
    const flagged = el.required || el.getAttribute('aria-required') === 'true' || el.getAttribute('aria-invalid') === 'true' || groupMarkedRequired(el);
    if (flagged && empty(el)) { const n = nameOf(el); if (n && !out.includes(n)) out.push(n); }
  }
  return out;
}"""


def easy_apply_module():
    from jobpilot.agents.apply import easy_apply

    return easy_apply


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

        def _button(self, scope: Any, name: Any) -> Any:
            found = super()._button(scope, name)
            if found is None and scope is self.page and name.pattern == easy_apply_module().EASY_APPLY_BUTTON.pattern:
                if DRAFT_NOTICE.search(self._body_text()):
                    return super()._button(scope, CONTINUE_BUTTON)   # resume the draft saved earlier
            return found

        @classmethod
        def _error_questions(cls, dialog: Any) -> list[str]:
            """LinkedIn's flagged errors, plus any required control still empty, each by name."""
            names: list[str] = []
            try:
                names = list(super()._error_questions(dialog))
            except Exception:
                pass
            try:
                names += [cls._clean(n) for n in dialog.evaluate(UNANSWERED_REQUIRED_JS)]
            except Exception:
                pass
            return [n for i, n in enumerate(names) if n and n not in names[:i]]

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
