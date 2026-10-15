"""
Naukri one-click apply in the agent's own signed-in browser profile.

One-time sign-in (you type your password; the agent never sees it):
    python -m jobpilot.agents.apply.login naukri

JobPilot's apply agent subclasses NaukriApplier and reuses the selectors below.

How Naukri applies: on a job page, the "Apply" button submits your Naukri
profile straight away. Some employers add a chatbot with screening
questions; those are answered only from config/application_profile.yaml,
and the application is abandoned (recorded as "Answers needed (Naukri)")
the moment a question has no approved answer. Jobs that send you to the
company's own site are recorded as "External apply (Naukri)" and skipped.

Naukri changes its page markup from time to time. Every selector lives in
the constants below so it can be fixed in one place, and nothing is
recorded as applied unless Naukri shows its own confirmation.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from playwright.sync_api import BrowserContext, Locator, Page

from jobpilot.agents.apply.easy_apply import EasyApplyResult, answer_for

logger = logging.getLogger(__name__)

LOGIN_URL = "https://www.naukri.com/nlogin/login"
SESSION_COOKIES = ("nauk_at", "nauk_rt")
SECURITY_MARKERS = ("captcha", "/nlogin", "verify-otp")

APPLY_BUTTON = "#apply-button, button:text-matches('^ *apply *$', 'i')"
COMPANY_SITE_BUTTON = "#company-site-button, button:has-text('Apply on company site')"
ALREADY_APPLIED_BUTTON = "#already-applied, button:text-matches('^ *applied *$', 'i')"
LOGIN_PROMPT = re.compile(r"login to apply|register to apply", re.IGNORECASE)
APPLIED_TEXT = re.compile(
    r"you have successfully applied|successfully applied|applied to\b|application (?:sent|submitted)",
    re.IGNORECASE,
)
CHATBOT = "[class*='chatbot_Drawer'], [class*='chatbot_drawer'], .chatbot_DrawerContentWrapper"
BOT_MESSAGE = "[class*='botMsg'], .botItem"
CHAT_INPUT = "[contenteditable='true'], .chatbot_InputContainer input, textarea"
CHAT_RADIO = "input[type='radio'], input[type='checkbox']"
CHAT_SAVE = "[class*='sendMsg'], button:has-text('Save'), button:has-text('Submit')"

SUBMITTED = "Submitted"
ALREADY_APPLIED = "Already applied"
EXTERNAL = "External apply (Naukri)"
ANSWERS_NEEDED = "Answers needed (Naukri)"
NOT_CONFIRMED = "Apply not confirmed (Naukri)"
NOT_SIGNED_IN = "Not signed in (Naukri)"
MAX_CHAT_QUESTIONS = 10


class NaukriSecurityError(RuntimeError):
    """Naukri asked for sign-in, OTP or CAPTCHA."""


def is_signed_in(browser: BrowserContext) -> bool:
    return any(
        cookie["name"] in SESSION_COOKIES and cookie["value"]
        for cookie in browser.cookies("https://www.naukri.com")
    )


def _check_security(page: Page) -> None:
    if any(marker in page.url.lower() for marker in SECURITY_MARKERS):
        raise NaukriSecurityError(
            f"Naukri showed a sign-in or verification page ({page.url}). "
            "Run 'python -m jobpilot.agents.apply.login naukri' and complete it yourself."
        )


def _visible(page_or_scope: Any, selector: str) -> Locator | None:
    for candidate in page_or_scope.locator(selector).all():
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def _body(page: Page) -> str:
    try:
        return page.locator("body").inner_text(timeout=5_000)
    except Exception:
        return ""


class NaukriApplier:
    def __init__(self, page: Page, answers: dict[str, str]) -> None:
        self.page = page
        self.answers = answers

    def apply(self) -> EasyApplyResult:
        page = self.page
        _check_security(page)
        if _visible(page, ALREADY_APPLIED_BUTTON) is not None and _visible(page, APPLY_BUTTON) is None:
            return EasyApplyResult(ALREADY_APPLIED)
        if _visible(page, COMPANY_SITE_BUTTON) is not None and _visible(page, APPLY_BUTTON) is None:
            return EasyApplyResult(EXTERNAL)
        if LOGIN_PROMPT.search(_body(page)):
            return EasyApplyResult(NOT_SIGNED_IN)

        button = _visible(page, APPLY_BUTTON)
        if button is None:
            return EasyApplyResult(EXTERNAL)
        button.click()
        page.wait_for_timeout(3_000)
        _check_security(page)

        result = EasyApplyResult(NOT_CONFIRMED)
        for _ in range(MAX_CHAT_QUESTIONS):
            if self._confirmed():
                result.status = SUBMITTED
                return result
            chatbot = _visible(page, CHATBOT)
            if chatbot is None:
                break
            if not self._answer_one(chatbot, result):
                result.status = ANSWERS_NEEDED
                return result
            page.wait_for_timeout(2_000)

        page.wait_for_timeout(2_000)
        if self._confirmed():
            result.status = SUBMITTED
        return result

    def _confirmed(self) -> bool:
        if APPLIED_TEXT.search(_body(self.page)):
            return True
        return _visible(self.page, ALREADY_APPLIED_BUTTON) is not None and _visible(self.page, APPLY_BUTTON) is None

    def _answer_one(self, chatbot: Locator, result: EasyApplyResult) -> bool:
        messages = chatbot.locator(BOT_MESSAGE).all()
        question = " ".join(messages[-1].inner_text().split()) if messages else ""
        if not question:
            return False
        answer = answer_for(question, self.answers)
        if not answer:
            result.unknown_questions.append(question)
            return False

        options = [option for option in chatbot.locator(CHAT_RADIO).all() if self._option_label(chatbot, option)]
        if options:
            for option in options:
                if self._option_label(chatbot, option).lower() == answer.lower():
                    self._click_option(chatbot, option)
                    break
            else:
                result.unknown_questions.append(question)
                return False
        else:
            box = _visible(chatbot, CHAT_INPUT)
            if box is None:
                result.unknown_questions.append(question)
                return False
            box.click()
            if box.evaluate("e => 'value' in e"):
                box.fill(answer)
            else:
                box.type(answer, delay=40)

        save = _visible(chatbot, CHAT_SAVE)
        if save is None:
            result.unknown_questions.append(question)
            return False
        save.click()
        result.filled_fields.append(question)
        return True

    @staticmethod
    def _option_label(scope: Locator, option: Locator) -> str:
        option_id = option.get_attribute("id") or ""
        if option_id:
            label = scope.locator(f"label[for='{option_id}']")
            if label.count():
                return " ".join(label.first.inner_text().split())
        return (option.get_attribute("value") or "").strip()

    @staticmethod
    def _click_option(scope: Locator, option: Locator) -> None:
        option_id = option.get_attribute("id") or ""
        label = scope.locator(f"label[for='{option_id}']") if option_id else None
        if label is not None and label.count():
            label.first.click()
        else:
            option.check(force=True)
