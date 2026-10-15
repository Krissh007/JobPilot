"""
Naukri apply: the legacy NaukriApplier (selectors, chatbot handling, Naukri's own
confirmation check), split in two so nothing is sent before you decide.

On Naukri the "Apply" button IS the submit; screening questions (a chatbot) come after it.
- precheck(): read the posting only: signed in? external? already applied? Never clicks.
- submit():   click Apply, then answer the chatbot ONLY from your answer bank / profile.
  A question the bank can't answer, or any consent/terms question, stops it and leaves the
  chat open for you. Gemini drafts are never typed into Naukri.
"""

from __future__ import annotations

from typing import Any

from jobpilot.agents.apply.answers import AnswerBank
from jobpilot.agents.apply.filler import FieldRecord, is_consent

READY = "ready"

# Find the "Follow <company> as you apply" text, then the checkbox beside it (input or a role=checkbox widget).
UNTICK_FOLLOW_JS = r"""() => {
  const label = [...document.querySelectorAll('label, span, p, div')]
    .filter(e => /follow .{1,120} as you apply/i.test(e.textContent || '') && (e.textContent || '').length < 200).pop();
  if (!label) return 'not found';
  let scope = label;
  for (let i = 0; i < 3 && scope; i++, scope = scope.parentElement) {
    const box = scope.querySelector("input[type='checkbox'], [role='checkbox']");
    if (!box) continue;
    const ticked = box.type === 'checkbox' ? box.checked : box.getAttribute('aria-checked') === 'true';
    if (!ticked) return 'not ticked';
    box.click();
    const still = box.type === 'checkbox' ? box.checked : box.getAttribute('aria-checked') === 'true';
    return still ? 'left ticked' : 'unticked';
  }
  return 'left ticked';
}"""


def naukri_module():
    from jobpilot.agents.apply import naukri_applier

    return naukri_applier


def make_naukri_applier():
    nk = naukri_module()

    class JobPilotNaukri(nk.NaukriApplier):
        def __init__(self, page: Any, bank: AnswerBank) -> None:
            super().__init__(page, answers={})
            self.bank = bank
            self.records: list[FieldRecord] = []
            self.consent: list[str] = []
            self.unknown: list[str] = []
            self.follow = ""  # what untick_follow() did, shown in Review

        def precheck(self) -> str:
            """READY, or the legacy ALREADY_APPLIED / EXTERNAL / NOT_SIGNED_IN status. Never clicks."""
            page = self.page
            nk._check_security(page)
            apply_button = nk._visible(page, nk.APPLY_BUTTON)
            if nk._visible(page, nk.ALREADY_APPLIED_BUTTON) is not None and apply_button is None:
                return nk.ALREADY_APPLIED
            if nk.LOGIN_PROMPT.search(nk._body(page)):
                return nk.NOT_SIGNED_IN
            if apply_button is None:  # only "Apply on company site", or no apply at all
                return nk.EXTERNAL
            return READY

        def submit(self) -> str:
            """Click Apply and work through the chatbot. SUBMITTED, ANSWERS_NEEDED or NOT_CONFIRMED."""
            page = self.page
            button = nk._visible(page, nk.APPLY_BUTTON)
            if button is None:
                return nk.NOT_CONFIRMED
            self.follow = self.untick_follow()
            button.click()
            page.wait_for_timeout(3_000)
            nk._check_security(page)
            result = nk.EasyApplyResult(nk.NOT_CONFIRMED)
            for _ in range(nk.MAX_CHAT_QUESTIONS):
                if self._confirmed():
                    return nk.SUBMITTED
                chatbot = nk._visible(page, nk.CHATBOT)
                if chatbot is None:
                    break
                if not self._answer_one(chatbot, result):
                    return nk.ANSWERS_NEEDED
                page.wait_for_timeout(2_000)
            page.wait_for_timeout(2_000)
            return nk.SUBMITTED if self._confirmed() else nk.NOT_CONFIRMED

        def confirmed(self, _page: Any = None) -> bool:
            try:
                return self._confirmed()
            except Exception:
                return False

        def untick_follow(self) -> str:
            """Naukri pre-ticks 'Follow <company> as you apply'. Best effort, never fatal:
            'unticked', 'not ticked', 'not found' or 'left ticked' (couldn't find its checkbox)."""
            try:
                return self.page.evaluate(UNTICK_FOLLOW_JS) or "not found"
            except Exception:
                return "not found"

        def _awaits_answer(self, chatbot: Any) -> bool:
            if nk._visible(chatbot, nk.CHAT_INPUT) is not None:
                return True
            return any(option.is_visible() for option in chatbot.locator(nk.CHAT_RADIO).all())

        def _answer_one(self, chatbot: Any, result: Any) -> bool:
            messages = chatbot.locator(nk.BOT_MESSAGE).all()
            question = " ".join(messages[-1].inner_text().split()) if messages else ""
            if not self._awaits_answer(chatbot):
                return True  # a plain message ("Thank you for your responses."): wait for Naukri's confirmation
            if not question:
                return False
            if is_consent(question):
                self.consent.append(question)
                return False
            hit = self.bank.lookup(question)
            if hit is None:
                self.bank.note_unknown(question)
                self.unknown.append(question)
                return False
            # the legacy code picks the option / types it; its matcher wants lower-cased, space-normalised keys
            self.answers = {" ".join(question.lower().split()): hit.value}
            if not super()._answer_one(chatbot, result):
                if question not in self.unknown:
                    self.unknown.append(question)
                return False
            self.records.append(FieldRecord(len(self.records) + 1, question, hit.value, hit.source, False))
            return True

    return JobPilotNaukri
