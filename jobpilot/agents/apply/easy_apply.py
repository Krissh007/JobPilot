"""
LinkedIn Easy Apply form filler.

Fills the Easy Apply dialog on the page that is already open and moves
through its steps, then STOPS on the final step. prepare() never clicks
"Submit application"; only submit(), called by the opt-in auto-apply mode
for fully answered forms, does. Fields that already have a value (LinkedIn pre-fills contact
details from the account) are left alone.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from playwright.sync_api import Locator, Page

# Result statuses, also stored in the applications table.
READY_TO_SUBMIT = "Ready to submit (Easy Apply)"
ANSWERS_NEEDED = "Answers needed (Easy Apply)"
NOT_EASY_APPLY = "Not Easy Apply"
ALREADY_APPLIED = "Already applied"
SUBMITTED = "Submitted"
STOPPED = "Stopped: unexpected form"

EASY_APPLY_BUTTON = re.compile(r"^\s*easy apply\b", re.IGNORECASE)
SUBMIT_BUTTON = re.compile(r"submit application", re.IGNORECASE)
REVIEW_BUTTON = re.compile(r"^\s*review\b|review your application", re.IGNORECASE)
NEXT_BUTTON = re.compile(r"^\s*next\b|continue to next step", re.IGNORECASE)
ALREADY_APPLIED_TEXT = re.compile(r"\bapplied\s+\d+\s+\w+\s+ago\b|application submitted", re.IGNORECASE)
APPLICATION_SENT_TEXT = re.compile(
    r"your application was sent|application sent|application submitted",
    re.IGNORECASE,
)


@dataclass
class EasyApplyResult:
    status: str
    filled_fields: list[str] = field(default_factory=list)
    unknown_questions: list[str] = field(default_factory=list)
    steps: int = 0

    @property
    def needs_candidate(self) -> bool:
        # A form the filler could not finish still needs the candidate.
        return self.status in (READY_TO_SUBMIT, ANSWERS_NEEDED, STOPPED)


def build_answers(application_profile: dict[str, Any]) -> dict[str, str]:
    """
    Flatten application_profile.yaml into {question fragment: answer}.
    Screening answers win over contact/application fields.
    """
    contact = application_profile.get("contact", {}) or {}
    application = application_profile.get("application", {}) or {}
    full_name = str(contact.get("full_name") or "").strip()
    first, _, last = full_name.partition(" ")

    answers: dict[str, str] = {
        "first name": first,
        "last name": last,
        "full name": full_name,
        "email": contact.get("email", ""),
        "phone": contact.get("phone", ""),
        "mobile": contact.get("phone", ""),
        "city": contact.get("current_location", ""),
        "location": contact.get("current_location", ""),
        "notice period": application.get("notice_period_days", ""),
        "expected ctc": application.get("expected_ctc_lpa", ""),
        "expected salary": application.get("expected_ctc_lpa", ""),
        "relocate": application.get("willing_to_relocate", ""),
        "authorized to work": application.get("work_authorization_india", ""),
        "work authorization": application.get("work_authorization_india", ""),
    }
    for question, answer in (application_profile.get("screening_answers", {}) or {}).items():
        answers[str(question)] = answer

    return {
        " ".join(str(key).lower().split()): str(value).strip()
        for key, value in answers.items()
        if value not in (None, "") and str(value).strip()
    }


def answer_for(label: str, answers: dict[str, str]) -> str:
    """The answer whose key appears in the label; the longest key wins, so
    'years of experience with sql' beats 'years of experience'."""
    normalized = " ".join(label.lower().split())
    matches = [key for key in answers if key in normalized]
    if not matches:
        return ""
    return answers[max(matches, key=len)]


class EasyApplyFiller:
    def __init__(self, page: Page, answers: dict[str, str], max_steps: int = 8) -> None:
        self.page = page
        self.answers = answers
        self.max_steps = max_steps
        self._dialog: Locator | None = None

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def prepare(self) -> EasyApplyResult:
        """Open Easy Apply on the current job page and fill it up to submit."""
        body_text = self._body_text()
        if ALREADY_APPLIED_TEXT.search(body_text):
            return EasyApplyResult(ALREADY_APPLIED)

        button = self._button(self.page, EASY_APPLY_BUTTON)
        if button is None:
            return EasyApplyResult(NOT_EASY_APPLY)

        button.click()
        dialog = self._application_dialog()
        if dialog is None:
            return EasyApplyResult(STOPPED)
        self._dialog = dialog

        result = EasyApplyResult(STOPPED)
        for step in range(1, self.max_steps + 1):
            result.steps = step
            self.page.wait_for_timeout(800)
            self._fill_step(dialog, result)

            if self._button(dialog, SUBMIT_BUTTON) is not None:
                # Final step reached. Deliberately not clicked.
                result.status = ANSWERS_NEEDED if result.unknown_questions else READY_TO_SUBMIT
                return result

            advance = self._button(dialog, REVIEW_BUTTON) or self._button(dialog, NEXT_BUTTON)
            if advance is None:
                return result

            before = self._step_signature(dialog)
            advance.click()
            self.page.wait_for_timeout(1_200)

            if self._has_errors(dialog) or self._step_signature(dialog) == before:
                # LinkedIn kept us on this step: a required answer is missing.
                result.status = ANSWERS_NEEDED
                for question in self._error_questions(dialog):
                    if question not in result.unknown_questions:
                        result.unknown_questions.append(question)
                return result

        return result

    def submit(self, timeout_ms: int = 15_000) -> bool:
        """
        Click "Submit application" on a form that prepare() left READY_TO_SUBMIT
        (every required field answered from approved answers). Used only by
        the opt-in auto-apply mode. True once LinkedIn confirms.
        """
        if self._dialog is None:
            return False
        button = self._button(self._dialog, SUBMIT_BUTTON)
        if button is None:
            return False
        self._untick_follow_company(self._dialog)
        button.click()
        waited = 0
        while waited <= timeout_ms:
            self.page.wait_for_timeout(1_000)
            waited += 1_000
            if self.application_sent(self.page):
                return True
        return False

    @staticmethod
    def _untick_follow_company(dialog: Locator) -> None:
        try:
            for box in dialog.locator("input[type='checkbox']").all():
                label = EasyApplyFiller._label_for(dialog, box).lower()
                if "follow" in label and box.is_checked():
                    box.uncheck(force=True)
        except Exception:
            pass

    @staticmethod
    def application_sent(page: Page) -> bool:
        """True once LinkedIn shows its confirmation after the user submits."""
        try:
            return bool(APPLICATION_SENT_TEXT.search(page.locator("body").inner_text(timeout=2_000)))
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Filling
    # ------------------------------------------------------------------

    def _fill_step(self, dialog: Locator, result: EasyApplyResult) -> None:
        for fieldset in dialog.locator("fieldset").all():
            self._fill_radio_group(fieldset, result)

        for control in dialog.locator("input, select, textarea").all():
            kind = (control.get_attribute("type") or "").lower()
            if kind in ("radio", "checkbox", "hidden", "file", "submit", "button"):
                continue
            if not control.is_visible() or not control.is_editable():
                continue
            label = self._label_for(dialog, control)
            if not label:
                continue
            if self._current_value(control):
                continue  # pre-filled by LinkedIn or by an earlier step

            answer = answer_for(label, self.answers)
            if not answer:
                if self._is_required(control, label) and label not in result.unknown_questions:
                    result.unknown_questions.append(label)
                continue
            if self._set_value(control, answer):
                result.filled_fields.append(label)
            elif label not in result.unknown_questions:
                result.unknown_questions.append(label)

    def _fill_radio_group(self, fieldset: Locator, result: EasyApplyResult) -> None:
        radios = fieldset.locator("input[type='radio']")
        if radios.count() == 0 or not fieldset.is_visible():
            return
        question = self._clean(fieldset.locator("legend").first.inner_text()) if fieldset.locator("legend").count() else ""
        if not question:
            return
        if any(radio.is_checked() for radio in radios.all()):
            return

        answer = answer_for(question, self.answers)
        for radio in radios.all():
            option = self._label_for(fieldset, radio)
            if answer and option.lower() == answer.lower():
                self._choose(fieldset, radio)
                result.filled_fields.append(question)
                return
        if question not in result.unknown_questions:
            result.unknown_questions.append(question)

    @staticmethod
    def _choose(fieldset: Locator, radio: Locator) -> None:
        # LinkedIn hides the radio input and styles its label as the
        # clickable option, so click the label when the input is hidden.
        radio_id = radio.get_attribute("id") or ""
        label = fieldset.locator(f"label[for='{radio_id}']") if radio_id else None
        if not radio.is_visible() and label is not None and label.count():
            label.first.click()
        else:
            radio.check()

    def _set_value(self, control: Locator, answer: str) -> bool:
        tag = control.evaluate("element => element.tagName.toLowerCase()")
        try:
            if tag == "select":
                for option in control.locator("option").all():
                    text = self._clean(option.inner_text())
                    if text.lower() == answer.lower() or answer.lower() in text.lower():
                        control.select_option(label=option.inner_text())
                        return True
                return False
            control.fill(answer)
            return True
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Page helpers
    # ------------------------------------------------------------------

    def _application_dialog(self, timeout_ms: int = 10_000) -> Locator | None:
        """
        The dialog holding the application form. LinkedIn often has other
        dialogs open at the same time (promotions, empty overlays), so pick
        the visible one that has a Next, Review or Submit button.
        """
        waited = 0
        while waited <= timeout_ms:
            for dialog in self.page.get_by_role("dialog").all():
                try:
                    if not dialog.is_visible():
                        continue
                except Exception:
                    continue
                if any(
                    self._button(dialog, pattern) is not None
                    for pattern in (SUBMIT_BUTTON, REVIEW_BUTTON, NEXT_BUTTON)
                ):
                    return dialog
            self.page.wait_for_timeout(500)
            waited += 500
        return None

    def _body_text(self) -> str:
        try:
            return self.page.locator("main, body").first.inner_text(timeout=5_000)
        except Exception:
            return ""

    @staticmethod
    def _button(scope: Any, name: re.Pattern[str]) -> Locator | None:
        for candidate in scope.locator("button, a[role='button'], a").all():
            try:
                if not candidate.is_visible():
                    continue
                text = " ".join(
                    part
                    for part in (candidate.get_attribute("aria-label") or "", candidate.inner_text())
                    if part
                )
            except Exception:
                continue
            if name.search(text):
                return candidate
        return None

    @classmethod
    def _label_for(cls, scope: Locator, control: Locator) -> str:
        control_id = control.get_attribute("id") or ""
        if control_id:
            label = scope.locator(f"label[for='{control_id}']")
            if label.count():
                return cls._clean(label.first.inner_text())
        aria = control.get_attribute("aria-label") or ""
        if aria:
            return cls._clean(aria)
        labelled_by = (control.get_attribute("aria-labelledby") or "").split()
        texts = []
        for element_id in labelled_by:
            element = scope.page.locator(f"[id='{element_id}']")
            if element.count():
                texts.append(cls._clean(element.first.inner_text()))
        if any(texts):
            return " ".join(text for text in texts if text)
        wrapping = control.locator("xpath=ancestor::label[1]")
        if wrapping.count():
            return cls._clean(wrapping.first.inner_text())
        # LinkedIn's newer forms put the question in a label that is not
        # linked to its input; use the nearest container holding one.
        container = control.locator("xpath=ancestor::*[.//label or .//legend][1]")
        if container.count():
            label = container.first.locator("label, legend").first
            if label.count():
                return cls._clean(label.inner_text())
        return ""

    @staticmethod
    def _current_value(control: Locator) -> str:
        try:
            tag = control.evaluate("element => element.tagName.toLowerCase()")
            value = control.input_value().strip()
            if tag == "select" and value.lower().startswith("select"):
                return ""
            return value
        except Exception:
            return ""

    @staticmethod
    def _is_required(control: Locator, label: str) -> bool:
        return (
            control.get_attribute("required") is not None
            or (control.get_attribute("aria-required") or "").lower() == "true"
            or label.rstrip().endswith("*")
        )

    @staticmethod
    def _has_errors(dialog: Locator) -> bool:
        errors = dialog.locator("[role='alert'], .artdeco-inline-feedback--error")
        return any(error.is_visible() and error.inner_text().strip() for error in errors.all())

    @classmethod
    def _error_questions(cls, dialog: Locator) -> list[str]:
        questions: list[str] = []
        for error in dialog.locator("[role='alert'], .artdeco-inline-feedback--error").all():
            container = error.locator("xpath=ancestor::*[.//label or .//legend][1]")
            if container.count():
                text = container.first.locator("label, legend").first.inner_text()
                questions.append(cls._clean(text))
        return questions

    @staticmethod
    def _step_signature(dialog: Locator) -> str:
        try:
            return " ".join(dialog.inner_text().split())[:500]
        except Exception:
            return ""

    @staticmethod
    def _clean(text: str) -> str:
        # LinkedIn repeats label text for screen readers; keep the first line.
        lines = [" ".join(line.split()) for line in str(text).splitlines() if line.strip()]
        return lines[0] if lines else ""
