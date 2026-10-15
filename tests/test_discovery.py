from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlmodel import Session, select

from jobpilot.agents import discover
from jobpilot.db.models import AgentName, AgentRun, Job, JobStatus, RunStatus
from jobpilot.db.session import get_engine
from jobpilot.discovery.cards import Card, parse_experience, parse_posted
from jobpilot.discovery.config import DiscoveryConfig
from jobpilot.discovery.pipeline import build_queries, run_discovery
from jobpilot.discovery.prescore import prescore
from jobpilot.discovery.sources import linkedin as li
from jobpilot.discovery.sources import naukri as nk
from jobpilot.discovery.sources.base import Query, QueryBlocked
from jobpilot.discovery.titles import REGISTERED_TITLES, expand_keywords, matches_registered_title

TODAY = date(2026, 10, 14)


def card(title="Business Analyst", company="Acme", url="https://www.linkedin.com/jobs/view/4000000001", source="linkedin",
         age=1, **kw) -> Card:
    return Card(title=title, company=company, url=url, source=source, location="Hyderabad",
                posted_on=None if age is None else TODAY - timedelta(days=age), **kw)


class FakeSource:
    def __init__(self, name, by_keyword=None, fail=None, enrich_fail=False):
        self.name, self.by_keyword, self.fail = name, by_keyword or {}, fail or {}
        self.enrich_fail, self.enriched, self.fetched = enrich_fail, [], []

    def fetch_cards(self, query, max_cards, max_pages):
        self.fetched.append(query.keyword)
        if query.keyword in self.fail:
            raise self.fail[query.keyword]
        return list(self.by_keyword.get(query.keyword, []))[:max_cards]

    def enrich(self, c):
        if self.enrich_fail:
            raise QueryBlocked("blocked", "captcha")
        self.enriched.append(c.title)
        c.description = f"Full JD for {c.title}. " * 20
        return c


def config(**kw) -> DiscoveryConfig:
    base = dict(keywords=["Business Analyst"], use_variants=False, locations=["Hyderabad"], sources=["linkedin"])
    return DiscoveryConfig(**{**base, **kw})


# ---- titles / variants ---------------------------------------------------

def test_variants_added_after_configured_keywords_without_duplicates():
    out = expand_keywords(["Business Analyst", "AI Business Analyst"])
    assert out[:2] == ["Business Analyst", "AI Business Analyst"]
    assert {t.lower() for t in out} == {t.lower() for t in REGISTERED_TITLES}
    assert len(out) == len(REGISTERED_TITLES)


def test_queries_use_variants_only_when_enabled():
    assert len(build_queries(config(use_variants=False))) == 1
    assert len(build_queries(config(use_variants=True))) == len(REGISTERED_TITLES)


def test_registered_title_match_is_whole_word():
    assert matches_registered_title("Senior Business Analyst - Risk")
    assert matches_registered_title("Insights Analyst")
    assert not matches_registered_title("Analyst")
    assert not matches_registered_title("Business Analystics Lead")


# ---- parsing ----------------------------------------------------------------

def test_parse_posted_and_experience():
    assert parse_posted("3 days ago", TODAY) == TODAY - timedelta(days=3)
    assert parse_posted("2 weeks ago", TODAY) == TODAY - timedelta(days=14)
    assert parse_posted("Just now", TODAY) == TODAY
    assert parse_posted("2026-10-10", TODAY) == date(2026, 10, 10)
    assert parse_posted("", TODAY) is None
    assert parse_experience("3-5 Yrs") == (3, 5)
    assert parse_experience("4 years") == (4, None)
    assert parse_experience("") == (None, None)


# ---- pre-score ranks, never drops -------------------------------------------

def test_prescore_prefers_matching_fresh_titles():
    cfg = config(use_variants=True)
    good = card("AI Business Analyst", age=0, exp_min=3, exp_max=5)
    ok = card("Business Analyst", age=5)
    poor = card("Warehouse Supervisor", age=6, exp_min=10, exp_max=12)
    scores = [prescore(c, cfg, TODAY) for c in (good, ok, poor)]
    assert scores[0] > scores[1] > scores[2]
    assert 0 <= min(scores) and max(scores) <= 100


# ---- pipeline ---------------------------------------------------------------

def test_stale_cards_dropped_unknown_age_kept(session):
    src = FakeSource("linkedin", {"Business Analyst": [
        card("Business Analyst", url="https://www.linkedin.com/jobs/view/4000000001", age=2),
        card("Business Analyst", company="B", url="https://www.linkedin.com/jobs/view/4000000002", age=20),
        card("Business Analyst", company="C", url="https://www.linkedin.com/jobs/view/4000000003", age=None),
    ]})
    report = run_discovery(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    assert report.total("stale") == 1
    assert {j.company for j in session.exec(select(Job))} == {"Acme", "C"}
    assert src.enriched.count("Business Analyst") == 2  # stale card never enriched


def test_card_level_dedupe_within_run_across_queries_and_against_db(session):
    shared = "https://www.linkedin.com/jobs/view/4000000001"
    src = FakeSource("linkedin", {
        "Business Analyst": [card(url=shared), card(url=shared + "?trk=x")],       # same job twice in one query
        "Product Analyst": [card("Product Analyst", url=shared)],                  # and again from another query
    })
    cfg = config(keywords=["Business Analyst", "Product Analyst"])
    run_discovery(session, cfg, {"linkedin": src}, TODAY, log=lambda m: None)
    assert len(session.exec(select(Job)).all()) == 1
    assert src.enriched == ["Business Analyst"]          # enriched once

    again = FakeSource("linkedin", {"Business Analyst": [card(url=shared)]})
    report = run_discovery(session, config(), {"linkedin": again}, TODAY, log=lambda m: None)
    assert report.total("duplicate") == 1 and again.enriched == []   # already in the DB: no enrichment


def test_cross_portal_duplicate_by_company_title_city(session):
    li_card = card(url="https://www.linkedin.com/jobs/view/4000000001")
    nk_card = card(url="https://www.naukri.com/job-listings-business-analyst-acme-150926500001", source="naukri")
    srcs = {"linkedin": FakeSource("linkedin", {"Business Analyst": [li_card]}),
            "naukri": FakeSource("naukri", {"Business Analyst": [nk_card]})}
    report = run_discovery(session, config(sources=["linkedin", "naukri"]), srcs, TODAY, log=lambda m: None)
    assert report.total("duplicate") == 1 and len(session.exec(select(Job)).all()) == 1


def test_enrichment_budget_goes_to_best_prescore_but_all_survivors_are_saved(session):
    cards = [
        card("Warehouse Supervisor", company="W", url="https://www.linkedin.com/jobs/view/4000000010", age=1),
        card("AI Business Analyst", company="A", url="https://www.linkedin.com/jobs/view/4000000011", age=1),
        card("Business Analyst", company="B", url="https://www.linkedin.com/jobs/view/4000000012", age=3),
    ]
    src = FakeSource("linkedin", {"Business Analyst": cards})
    report = run_discovery(session, config(enrich_per_query=2), {"linkedin": src}, TODAY, log=lambda m: None)
    assert src.enriched == ["AI Business Analyst", "Business Analyst"]
    jobs = {j.company: j for j in session.exec(select(Job))}
    assert set(jobs) == {"W", "A", "B"}                       # the low-ranked card is kept, not dropped
    assert len(jobs["A"].jd_text) > 100 and jobs["W"].jd_text == ""
    assert all(j.status == JobStatus.found for j in jobs.values())
    assert report.total("enriched") == 2


def test_ties_go_to_newer_post(session):
    cards = [card(company="Old", url="https://www.linkedin.com/jobs/view/4000000021", age=5),
             card(company="New", url="https://www.linkedin.com/jobs/view/4000000022", age=0)]
    src = FakeSource("linkedin", {"Business Analyst": cards})
    run_discovery(session, config(enrich_per_query=1), {"linkedin": src}, TODAY, log=lambda m: None)
    assert [j.company for j in session.exec(select(Job).where(Job.jd_text != ""))] == ["New"]


def test_failed_query_does_not_kill_run(session):
    src = FakeSource("linkedin",
                     by_keyword={"Product Analyst": [card("Product Analyst", url="https://www.linkedin.com/jobs/view/4000000030")]},
                     fail={"Business Analyst": QueryBlocked("throttled", "HTTP 429"),
                           "Data Analyst": ValueError("selector changed")})
    cfg = config(keywords=["Business Analyst", "Data Analyst", "Product Analyst"])
    report = run_discovery(session, cfg, {"linkedin": src}, TODAY, log=lambda m: None)
    assert report.failed_queries == 2
    assert [r.blocked for r in report.results] == ["throttled", "", ""]
    assert report.results[1].error.startswith("ValueError")
    assert report.total("saved") == 1


def test_missing_source_fails_only_its_queries(session):
    cfg = config(sources=["naukri", "linkedin"])
    src = FakeSource("linkedin", {"Business Analyst": [card()]})
    report = run_discovery(session, cfg, {"linkedin": src}, TODAY, log=lambda m: None)
    assert report.failed_queries == 1 and report.total("saved") == 1


def test_blocked_enrichment_saves_remaining_as_cards(session):
    src = FakeSource("linkedin", {"Business Analyst": [card()]}, enrich_fail=True)
    report = run_discovery(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    assert report.total("saved") == 1 and report.total("enriched") == 0
    assert report.failed_queries == 0


# ---- sources ---------------------------------------------------------------------

LINKEDIN_HTML = """
<ul><li><div class="base-card base-search-card job-search-card" data-entity-urn="urn:li:jobPosting:4012345678">
 <a class="base-card__full-link" href="https://in.linkedin.com/jobs/view/ai-business-analyst-at-acme-4012345678?trk=x"></a>
 <h3 class="base-search-card__title"> AI Business Analyst </h3>
 <h4 class="base-search-card__subtitle"><a>Acme &amp; Co</a></h4>
 <span class="job-search-card__location">Hyderabad, Telangana, India</span>
 <time class="job-search-card__listdate" datetime="2026-10-12">2 days ago</time></div></li>
<li><div class="base-card base-search-card" data-entity-urn="urn:li:jobPosting:4099999999">
 <h3 class="base-search-card__title">Product Analyst</h3><h4 class="base-search-card__subtitle">Beta</h4>
 <span class="job-search-card__location">Hyderabad</span></div></li></ul>"""


def test_linkedin_card_parsing():
    cards = li.parse_cards(LINKEDIN_HTML, TODAY)
    assert [(c.title, c.company, c.url) for c in cards] == [
        ("AI Business Analyst", "Acme & Co", "https://www.linkedin.com/jobs/view/4012345678"),
        ("Product Analyst", "Beta", "https://www.linkedin.com/jobs/view/4099999999")]
    assert cards[0].posted_on == date(2026, 10, 12) and cards[1].posted_on is None


def test_linkedin_description_parsing():
    markup = '<div class="show-more-less-html__markup">Own the roadmap<br>SQL &amp; Python<ul><li>A</li><li>B</li></ul></div>'
    assert li.parse_description(markup) == "Own the roadmap\nSQL & Python\n- A\n- B"


class FakeRequest:
    def __init__(self, *statuses_and_bodies):
        self.queue, self.urls = list(statuses_and_bodies), []

    def get(self, url, timeout=0):
        self.urls.append(url)
        status, body = self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]
        return SimpleNamespace(status=status, text=lambda: body)


def test_linkedin_search_url_sorts_by_date_and_limits_window():
    url = li.LinkedInSource(None).search_url(Query("linkedin", "Business Analyst", "Hyderabad", 7), 10)
    assert "sortBy=DD" in url and "f_TPR=r604800" in url and "start=10" in url and "Hyderabad%2C+India" in url
    assert "f_AL=true" in li.LinkedInSource(None, easy_apply_only=True).search_url(Query("linkedin", "x", "Hyderabad", 7), 0)


def test_linkedin_throttle_retries_then_blocks_query():
    req = FakeRequest((429, ""))
    waits = []
    source = li.LinkedInSource(req, sleep_ms=waits.append, today=lambda: TODAY)
    with pytest.raises(QueryBlocked) as err:
        source.fetch_cards(Query("linkedin", "x", "Hyderabad", 7), 10, 2)
    assert err.value.kind == "throttled" and len(req.urls) == 3        # first try + 2 backoffs
    assert 20_000 in waits and 60_000 in waits


def test_linkedin_999_blocks_query_but_later_page_failure_keeps_earlier_cards():
    with pytest.raises(QueryBlocked):
        li.LinkedInSource(FakeRequest((999, ""))).fetch_cards(Query("linkedin", "x", "Hyderabad", 7), 10, 2)

    full = "".join(
        f'<div class="base-search-card" data-entity-urn="urn:li:jobPosting:40000000{i:02d}"><h3 class="base-search-card__title">T{i}</h3></div>'
        for i in range(10))
    cards = li.LinkedInSource(FakeRequest((200, full), (999, "")), today=lambda: TODAY).fetch_cards(
        Query("linkedin", "x", "Hyderabad", 7), 50, 3)
    assert len(cards) == 10


def test_naukri_url_and_card_mapping():
    source = nk.NaukriSource(None, "sort=f")
    q = Query("naukri", "AI Business Analyst", "Hyderabad", 7)
    assert source.search_url(q) == "https://www.naukri.com/ai-business-analyst-jobs-in-hyderabad?jobAge=7&sort=f"
    assert source.search_url(q, 3).startswith("https://www.naukri.com/ai-business-analyst-jobs-in-hyderabad-3?")
    assert nk.NaukriSource(None, "").search_url(q).endswith("?jobAge=7")
    cards = nk.results_to_cards([
        {"title": "Product Analyst", "url": "https://www.naukri.com/job-listings-product-analyst-x-150926500009",
         "company": "X", "location": "Hyderabad", "experience": "3-5 Yrs", "posted": "3 days ago", "snippet": "SQL"},
        {"title": "", "url": "u"}], TODAY)
    assert len(cards) == 1 and (cards[0].exp_min, cards[0].exp_max) == (3, 5)
    assert cards[0].posted_on == TODAY - timedelta(days=3)


# ---- agent entry point -----------------------------------------------------------

def test_agent_run_records_run_and_saves_found(db_url):
    src = FakeSource("linkedin", {"Business Analyst": [card(url="https://www.linkedin.com/jobs/view/4000000050")]})
    discover.run({"keywords": ["Business Analyst"], "use_variants": False, "sources": ["linkedin"]},
                 db_url=db_url, sources={"linkedin": src})
    with Session(get_engine(db_url)) as s:
        run = s.exec(select(AgentRun)).one()
        assert run.agent == AgentName.discovery and run.status == RunStatus.ok and run.found == 1 and run.processed == 1
        assert s.exec(select(Job)).one().status == JobStatus.found


def test_dry_run_opens_no_browser_and_saves_nothing(db_url):
    discover.run({"keywords": ["Business Analyst"], "sources": ["linkedin"]}, dry_run=True, db_url=db_url)
    with Session(get_engine(db_url)) as s:
        assert s.exec(select(Job)).all() == []
        assert "AI business analyst" in s.exec(select(AgentRun)).one().log


# ---- experience from JD, enrichment backlog ------------------------------------------

from jobpilot.db.models import Actor, StatusHistory  # noqa: E402
from jobpilot.db.repo import upsert_job  # noqa: E402
from jobpilot.discovery.backlog import EXPIRED_PREFIX, run_backlog  # noqa: E402
from jobpilot.experience import experience_from_jd  # noqa: E402


def test_experience_from_jd():
    assert experience_from_jd("Requires 4-6 years of experience in analytics") == (4, 6)
    assert experience_from_jd("Minimum 5 years experience with SQL") == (5, None)
    assert experience_from_jd("3+ years of relevant experience") == (3, None)
    assert experience_from_jd("Founded 12 years ago. Great culture.") == (None, None)
    assert experience_from_jd("") == (None, None)


def test_pipeline_reads_experience_from_jd_after_enrichment_and_stores_posted_on(session):
    class JDSource(FakeSource):
        def enrich(self, c):
            c.description = "Own roadmap. 8-12 years of experience required. " * 5
            return c

    src = JDSource("linkedin", {"Business Analyst": [card(age=2)]})
    run_discovery(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    job = session.exec(select(Job)).one()
    assert (job.exp_min, job.exp_max) == (8, 12) and job.posted_on == TODAY - timedelta(days=2)


def add_found(session, n, title="Business Analyst", jd="", posted=None, found_days_ago=0, **kw):
    job, _ = upsert_job(session, {"url": f"https://www.linkedin.com/jobs/view/40000001{n:02d}", "title": title,
                                  "company": f"Co{n}", "source": "linkedin", "location": "Hyderabad",
                                  "jd_text": jd, "posted_on": posted, **kw})
    job.found_at = job.found_at - timedelta(days=found_days_ago)
    session.add(job)
    session.commit()
    return job


class BacklogSource(FakeSource):
    def enrich(self, c):
        if self.enrich_fail:
            raise QueryBlocked("blocked", "captcha")
        self.enriched.append(c.url)
        c.description = "" if "EMPTY" in c.title else "Needs 4-6 years of experience. SQL, LLM work. " * 5
        return c


def test_backlog_enriches_freshest_first_within_budget(session):
    old = add_found(session, 1, posted=TODAY - timedelta(days=5))
    fresh = add_found(session, 2, posted=TODAY)
    mid = add_found(session, 3, posted=TODAY - timedelta(days=2))
    src = BacklogSource("linkedin")
    report = run_backlog(session, config(backlog_budget=2), {"linkedin": src}, TODAY, log=lambda m: None)
    assert src.enriched == [fresh.url, mid.url]
    assert report.enriched == 2 and report.waiting == 1
    session.refresh(fresh)
    session.refresh(old)
    assert len(fresh.jd_text) > 100 and (fresh.exp_min, fresh.exp_max) == (4, 6) and old.jd_text == ""


def test_backlog_expires_jobs_past_freshness_window(session):
    stale = add_found(session, 1, posted=TODAY - timedelta(days=9))
    live = add_found(session, 2, posted=TODAY - timedelta(days=7))
    src = BacklogSource("linkedin")
    report = run_backlog(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    session.refresh(stale)
    assert report.expired == 1 and stale.status == JobStatus.rejected and EXPIRED_PREFIX in stale.notes
    assert src.enriched == [live.url]                       # the expired job is never opened
    hist = session.exec(select(StatusHistory).where(StatusHistory.job_id == stale.id,
                                                    StatusHistory.to_status == JobStatus.rejected)).one()
    assert hist.actor == Actor.agent


def test_backlog_falls_back_to_found_date_when_posted_unknown(session):
    old = add_found(session, 1, posted=None, found_days_ago=10)
    new = add_found(session, 2, posted=None, found_days_ago=1)
    run_backlog(session, config(), {"linkedin": BacklogSource("linkedin")}, date.today(), log=lambda m: None)
    assert session.get(Job, old.id).status == JobStatus.rejected and session.get(Job, new.id).status == JobStatus.found
    assert len(session.get(Job, new.id).jd_text) > 100


def test_backlog_skips_enriched_scored_and_decided_jobs(session):
    add_found(session, 1, jd="x" * 400, posted=TODAY)
    scored = add_found(session, 2, posted=TODAY)
    scored.score = 50
    session.add(scored)
    applied = add_found(session, 3, posted=TODAY, status=JobStatus.applied)
    session.commit()
    src = BacklogSource("linkedin")
    report = run_backlog(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    assert src.enriched == [] and report.enriched == 0 and applied.status == JobStatus.applied


def test_backlog_empty_description_not_retried_and_block_stops_that_source(session):
    empty = add_found(session, 1, title="EMPTY role", posted=TODAY)
    src = BacklogSource("linkedin")
    run_backlog(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    run_backlog(session, config(), {"linkedin": src}, TODAY, log=lambda m: None)
    assert src.enriched == [empty.url] and "Enrich:" in session.get(Job, empty.id).notes

    a = add_found(session, 2, posted=TODAY)
    add_found(session, 3, posted=TODAY)
    report = run_backlog(session, config(), {"linkedin": BacklogSource("linkedin", enrich_fail=True)}, TODAY,
                         log=lambda m: None)
    assert report.enriched == 0 and report.waiting == 2 and session.get(Job, a.id).status == JobStatus.found


def test_backlog_runs_after_discovery_in_agent(db_url):
    with Session(get_engine(db_url)) as s:
        add_found(s, 5, posted=date.today())
    discover.run({"keywords": ["Business Analyst"], "use_variants": False, "sources": ["linkedin"]}, db_url=db_url,
                 sources={"linkedin": BacklogSource("linkedin", {})})
    with Session(get_engine(db_url)) as s:
        assert len(s.exec(select(Job)).one().jd_text) > 100
        assert "Backlog: expired 0, enriched 1" in s.exec(select(AgentRun)).one().log


# ---- Naukri pages by clicking Next (the URL page number is ignored by the site) ----------------

class FakeNaukriPage:
    """Results page whose address-bar page number is ignored, like the real site: only the Next button advances."""

    def __init__(self, pages, next_after_last=False):
        self.pages, self.index, self.gotos, self.clicks = pages, 0, [], 0
        self.next_after_last = next_after_last
        self.url = "https://www.naukri.com/x"

    def goto(self, url, **_kw):
        self.gotos.append(url)
        self.index = 0                      # whatever page number is in the URL, page 1 is shown

    def wait_for_timeout(self, _ms):
        pass

    def evaluate(self, _js):
        return self.pages[self.index]

    def locator(self, selector):
        page = self

        class Loc:
            last = property(lambda self: self)

            def count(self):
                return 1 if selector in ("a[aria-label='Next']", "body") and (page.index + 1 < len(page.pages) or page.next_after_last) else 0

            def is_visible(self):
                return True

            def get_attribute(self, _name):
                return None

            def click(self, **_kw):
                page.clicks += 1
                page.index = min(page.index + 1, len(page.pages) - 1)

            def inner_text(self, **_kw):
                return ""

        return Loc()


def naukri_rows(prefix, n):
    return [{"title": f"Business Analyst {prefix}{i}", "company": "Co", "url": f"https://www.naukri.com/job-listings-{prefix}{i}",
             "location": "Hyderabad", "experience": "3-5 Yrs", "posted": "1 day ago", "snippet": ""} for i in range(n)]


def test_naukri_reaches_later_pages_by_clicking_next():
    page = FakeNaukriPage([naukri_rows("a", 20), naukri_rows("b", 20), naukri_rows("c", 20)])
    cards = nk.NaukriSource(page, "sort=f", today=lambda: TODAY).fetch_cards(Query("naukri", "Business Analyst", "Hyderabad", 7), 60, 5)
    assert len(cards) == 60 and page.clicks == 2 and len(page.gotos) == 1


def test_naukri_stops_when_there_is_no_next_button():
    page = FakeNaukriPage([naukri_rows("a", 20), naukri_rows("b", 7)])
    cards = nk.NaukriSource(page, "", today=lambda: TODAY).fetch_cards(Query("naukri", "Business Analyst", "Hyderabad", 7), 60, 5)
    assert len(cards) == 27 and page.clicks == 1


def test_naukri_stops_when_next_shows_nothing_new():
    page = FakeNaukriPage([naukri_rows("a", 20), naukri_rows("a", 20)])          # next re-shows the same cards
    cards = nk.NaukriSource(page, "", today=lambda: TODAY).fetch_cards(Query("naukri", "Business Analyst", "Hyderabad", 7), 60, 5)
    assert len(cards) == 20


# ---- role family in the pre-score ---------------------------------------------------------------

def test_prescore_ranks_core_roles_above_engineering_titles_from_broad_variants():
    cfg = config(use_variants=True)
    core = prescore(card("Business Analyst", age=2), cfg, TODAY)
    analytics = prescore(card("Sr. Consultant - Analytics", age=2), cfg, TODAY)
    engineer = prescore(card("Data Engineer", age=2), cfg, TODAY)
    tester = prescore(card("Automation Test Engineer", age=2), cfg, TODAY)
    assert core > engineer and analytics > engineer and core > tester
    assert 0 <= tester <= 100


def test_prescore_still_never_drops_an_off_role_card(session):
    cfg = config(use_variants=True)
    assert prescore(card("Senior Software Engineer", age=6, exp_min=10, exp_max=12), cfg, TODAY) >= 0
