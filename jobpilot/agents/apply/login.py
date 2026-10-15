"""
One-time sign-in to LinkedIn or Naukri in the apply agent's Chrome profile. You type the password; the agent never sees it.

    python -m jobpilot.agents.apply.login linkedin
    python -m jobpilot.agents.apply.login naukri
"""

from __future__ import annotations

import sys
import time

from jobpilot.agents.apply.runner import is_signed_in, signed_in_browser

LOGIN_URL = {"linkedin": "https://www.linkedin.com/login", "naukri": "https://www.naukri.com/nlogin/login"}
STILL_SIGNING_IN = {"linkedin": "checkpoint", "naukri": "nlogin"}


def login(portal: str, timeout_minutes: int = 10) -> bool:
    with signed_in_browser() as browser:
        if is_signed_in(browser, portal):
            print(f"Already signed in to {portal} in the agent profile.")
            return True
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.goto(LOGIN_URL[portal])
        print(f"Sign in to {portal} in the browser window (type your password or OTP there). Waiting up to {timeout_minutes} minutes...")
        deadline = time.monotonic() + timeout_minutes * 60
        while time.monotonic() < deadline:
            try:
                if is_signed_in(browser, portal) and STILL_SIGNING_IN[portal] not in page.url:
                    print("Signed in. The session is saved in the agent profile; you can close the window.")
                    time.sleep(2)
                    return True
            except Exception:
                print("The browser window was closed before sign-in finished. Run the command again.")
                return False
            time.sleep(2)
    print("Timed out before sign-in finished. Run the command again.")
    return False


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in LOGIN_URL:
        raise SystemExit("Usage: python -m jobpilot.agents.apply.login linkedin|naukri")
    sys.exit(0 if login(args[0]) else 1)


if __name__ == "__main__":
    main()
