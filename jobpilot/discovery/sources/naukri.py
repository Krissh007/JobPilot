"""
Naukri through a Playwright page. Cards come from one in-page extraction per
results page; descriptions are fetched on a second tab, only for enriched cards.
A CAPTCHA / login wall raises QueryBlocked (that query only); it is never bypassed.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from jobpilot.discovery.cards import Card, parse_experience, parse_posted
from jobpilot.discovery.sources.base import Query, QueryBlocked

BASE_URL = "https://www.naukri.com"
BLOCK_TERMS = ("captcha", "verify you are human", "access denied", "security check",
               "login to continue", "sign in to continue", "register to continue")
# Naukri is a single-page app: changing the page number in the URL re-shows page 1's results. Paging has to click Next.
NEXT_SELECTORS = ("a[aria-label='Next']", "button[aria-label='Next']", "a[rel='next']", "a:has-text('Next')", "button:has-text('Next')")
DETAIL_SELECTORS = ("section[class*='job-desc']", "div[class*='dang-inner-html']", "[class*='JDC']", "div.job-desc")

# Runs in the page: returns [{title, company, location, experience, posted, url, snippet}] for each result card.
EXTRACT_CARDS_JS = """
() => {
  const pick = (root, sels) => { for (const s of sels) { const e = root.querySelector(s); if (e && e.innerText.trim()) return e.innerText.trim(); } return ''; };
  const wrappers = ['div.srp-jobtuple-wrapper', 'article.jobTuple', "div[class*='jobTuple']"];
  let cards = [];
  for (const w of wrappers) { cards = [...document.querySelectorAll(w)]; if (cards.length) break; }
  return cards.map(c => {
    const link = c.querySelector("a.title, a[class*='title'], a[href*='/job-listings-']");
    const text = c.innerText || '';
    const m = text.match(/just (?:posted|now)|today|few hours? ago|\\d+\\+?\\s*(?:hours?|minutes?|mins?|days?|weeks?|months?)\\s+ago/i);
    return {
      title: link ? link.innerText.trim() : pick(c, ['h2']),
      url: link ? link.href : '',
      company: pick(c, ['a.comp-name', "a[class*='comp']", "[class*='company']"]),
      location: pick(c, ['span.locWdth', "[class*='location']"]),
      experience: pick(c, ['span.expwdth', "[class*='experience']"]),
      posted: m ? m[0] : '',
      snippet: pick(c, ['div.job-desc', "[class*='job-desc']"]),
    };
  });
}
"""


def _slug(value: str) -> str:
    return "-".join(value.strip().lower().replace("&", "and").replace("/", " ").replace("-", " ").split())


def results_to_cards(rows: list[dict], today: date) -> list[Card]:
    cards = []
    for row in rows:
        if not row.get("title") or not row.get("url"):
            continue
        exp_min, exp_max = parse_experience(row.get("experience", ""))
        cards.append(Card(
            title=row["title"], company=row.get("company", ""), url=row["url"], source="naukri",
            location=row.get("location", ""), posted_text=row.get("posted", ""),
            posted_on=parse_posted(row.get("posted", ""), today),
            experience_text=row.get("experience", ""), exp_min=exp_min, exp_max=exp_max,
            snippet=row.get("snippet", ""),
        ))
    return cards


class NaukriSource:
    name = "naukri"

    def __init__(self, page: Any, sort_param: str = "sort=f", today: Callable[[], date] = date.today) -> None:
        self.page = page
        self.sort_param = sort_param
        self.today = today
        self._detail_page: Any = None

    def search_url(self, query: Query, page_number: int = 1) -> str:
        path = f"{_slug(query.keyword)}-jobs-in-{_slug(query.location)}" + (f"-{page_number}" if page_number > 1 else "")
        params = [f"jobAge={query.max_posted_days}"] + ([self.sort_param] if self.sort_param else [])
        return f"{BASE_URL}/{path}?{'&'.join(params)}"

    def _blocked(self, page: Any) -> bool:
        try:
            text = page.locator("body").inner_text(timeout=5_000).lower()
        except Exception:
            return False
        return any(term in text or term in page.url.lower() for term in BLOCK_TERMS)

    def fetch_cards(self, query: Query, max_cards: int, max_pages: int) -> list[Card]:
        cards: list[Card] = []
        seen: set[str] = set()
        for number in range(1, max_pages + 1):
            if number == 1:
                self.page.goto(self.search_url(query, 1), wait_until="domcontentloaded", timeout=60_000)
                self.page.wait_for_timeout(3_000)
            elif not self._click_next():
                break
            if self._blocked(self.page):
                if cards:
                    break
                raise QueryBlocked("blocked", "CAPTCHA or login wall on the results page")
            batch = results_to_cards(self.page.evaluate(EXTRACT_CARDS_JS), self.today())
            fresh = [c for c in batch if c.key not in seen]
            if not fresh:
                break
            seen.update(c.key for c in fresh)
            cards.extend(fresh)
            if len(cards) >= max_cards:
                break
        return cards[:max_cards]

    def _click_next(self) -> bool:
        """Click the results page's Next button. False when there is none (last page) or it is disabled."""
        for selector in NEXT_SELECTORS:
            try:
                button = self.page.locator(selector).last
                if button.count() == 0 or not button.is_visible():
                    continue
                if button.get_attribute("disabled") is not None:
                    return False
                button.click(timeout=5_000)
                self.page.wait_for_timeout(3_000)
                return True
            except Exception:
                continue
        return False

    def enrich(self, card: Card) -> Card:
        if self._detail_page is None:
            self._detail_page = self.page.context.new_page()
        page = self._detail_page
        page.goto(card.url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_000)
        if self._blocked(page):
            raise QueryBlocked("blocked", "CAPTCHA or login wall on a job page")
        for selector in DETAIL_SELECTORS:
            locator = page.locator(selector).first
            if locator.count():
                text = " ".join(locator.inner_text(timeout=5_000).split())
                if text:
                    card.description = text[:20_000]
                    break
        return card

    def close(self) -> None:
        if self._detail_page is not None:
            self._detail_page.close()
            self._detail_page = None
