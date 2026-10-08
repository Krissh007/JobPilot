"""Windows desktop notifications (win11toast). Never fails the caller: no toast is better than a crash."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
DASHBOARD = "http://127.0.0.1:5173"


def notify(title: str, body: str, open_url: str | None = DASHBOARD) -> bool:
    try:
        from win11toast import notify as toast

        toast(title, body, app_id="JobPilot", on_click=open_url)
        return True
    except Exception as error:  # no notification service, headless session, etc.
        logger.info("Notification skipped (%s): %s | %s", error, title, body)
        return False
