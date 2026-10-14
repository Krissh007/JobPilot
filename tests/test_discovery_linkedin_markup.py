"""LinkedIn guest search markup as the real site serves it: nested elements whose class also starts with base-search-card."""

from __future__ import annotations

from datetime import date

from jobpilot.discovery.sources import linkedin as li

REAL_SHAPE = """<!DOCTYPE html>
        <li>
          <div class="base-card relative w-full hover:no-underline focus:no-underline
            base-card--link          base-search-card base-search-card--link job-search-card"
            data-entity-urn="urn:li:jobPosting:4475286356" data-impression-id="job-card" data-search-id="x">
            <a class="base-card__full-link absolute" href="https://in.linkedin.com/jobs/view/business-analyst-at-acme-4475286356?position=1&amp;pageNum=0"></a>
            <div class="search-entity-media"><img class="artdeco-entity-image" alt=""></div>
            <div class="base-search-card__info">
              <h3 class="base-search-card__title">
                Business Analyst
              </h3>
              <h4 class="base-search-card__subtitle">
                <a class="hidden-nested-link" href="https://in.linkedin.com/company/acme">
                  Acme &amp; Co
                </a>
              </h4>
              <div class="base-search-card__metadata">
                <span class="job-search-card__location">
                  Hyderabad, Telangana, India
                </span>
                <time class="job-search-card__listdate--new" datetime="2026-10-13">
                  1 day ago
                </time>
              </div>
            </div>
          </div>
        </li>
        <li>
          <div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4475000001">
            <div class="base-search-card__info">
              <h3 class="base-search-card__title">Senior Product Analyst</h3>
              <h4 class="base-search-card__subtitle"><a>Beta Labs</a></h4>
              <div class="base-search-card__metadata"><span class="job-search-card__location">Hyderabad</span></div>
            </div>
          </div>
        </li>"""


def test_nested_card_elements_do_not_split_a_card():
    cards = li.parse_cards(REAL_SHAPE, date(2026, 10, 14))
    assert [(c.title, c.company, c.location) for c in cards] == [
        ("Business Analyst", "Acme & Co", "Hyderabad, Telangana, India"),
        ("Senior Product Analyst", "Beta Labs", "Hyderabad")]
    assert cards[0].url == "https://www.linkedin.com/jobs/view/4475286356"
    assert cards[0].posted_on == date(2026, 10, 13) and cards[1].posted_on is None
