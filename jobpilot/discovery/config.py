"""Discovery settings. Stored in the settings table (key 'discovery', a partial dict); anything unset uses these defaults."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from jobpilot.discovery.titles import REGISTERED_TITLES, RELATED_TITLES


class DiscoveryConfig(BaseModel):
    keywords: list[str] = Field(default_factory=lambda: ["AI Business Analyst", "AI Product Analyst", "Business Analyst", "Product Analyst"])
    use_variants: bool = True                 # ignored while strict_titles is on
    strict_titles: bool = True                # search only `keywords`, and keep only cards whose title contains one of them
    variants: list[str] = Field(default_factory=lambda: list(REGISTERED_TITLES))
    related_titles: list[str] = Field(default_factory=lambda: list(RELATED_TITLES))
    locations: list[str] = Field(default_factory=lambda: ["Hyderabad"])
    sources: list[str] = Field(default_factory=lambda: ["naukri", "linkedin"])
    max_posted_days: int = Field(7, ge=1, le=60)
    max_cards_per_query: int = Field(120, ge=1)
    max_pages_per_query: int = Field(6, ge=1)
    enrich_per_query: int = Field(15, ge=0)   # the enrichment budget: full-description page loads per query
    backlog_budget: int = Field(100, ge=0)    # description page loads per run for leftover unenriched `found` jobs
    exp_min: int = 3                          # pre-score preference only; the hard gate belongs to the Scoring agent
    exp_max: int = 7
    linkedin_easy_apply_only: bool = False
    naukri_sort_param: str = "sort=f"         # empty string = no sort param

    def accepted_titles(self) -> list[str]:
        """Titles a card may carry under strict_titles: the keywords, the registered variants and the related titles."""
        return [*self.keywords, *self.variants, *self.related_titles]

    @field_validator("sources")
    @classmethod
    def _known_sources(cls, value: list[str]) -> list[str]:
        unknown = set(value) - {"naukri", "linkedin"}
        if unknown:
            raise ValueError(f"unknown sources: {sorted(unknown)}")
        return value

    @classmethod
    def from_settings(cls, session) -> "DiscoveryConfig":
        from jobpilot.db.repo import get_setting

        data = dict(get_setting(session, "discovery") or {})
        if "keywords" not in data:  # keep the Settings screen's search queries as the keyword source
            queries = get_setting(session, "search_queries")
            if queries:
                data["keywords"] = queries
        data.setdefault("linkedin_easy_apply_only", bool(get_setting(session, "linkedin_easy_apply_only")))
        return cls(**data)
