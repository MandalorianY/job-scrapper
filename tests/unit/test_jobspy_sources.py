from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

import scrapper.scrapers.jobspy_sources as jobspy_sources
from scrapper.config import SearchConfig
from scrapper.storage import JobStore


def test_default_config_uses_google_jobs_through_jobspy() -> None:
    assert "google" in SearchConfig().jobspy_sites


def test_google_jobspy_source_uses_google_search_term_and_records_rows(
    monkeypatch,
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "site": "google",
                    "id": "go-1",
                    "job_url": "https://company.example/jobs/1",
                    "job_url_direct": "https://company.example/apply/1",
                    "title": "Example coordinator",
                    "company": "Example",
                    "location": "Lyon",
                    "description": "Full-time example role.",
                    "date_posted": "2026-05-31",
                    "job_type": "CDI",
                }
            ]
        )

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Lyon, France",),
        jobspy_sites=("google",),
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        search_state = store.conn.execute("SELECT last_error FROM search_state WHERE source = 'google'").fetchone()
        canonical = store.canonical_dataframe()

        assert counts == {"google": 1}
        assert calls[0]["site_name"] == "google"
        assert calls[0]["location"] == "Lyon, France"
        assert "Lyon" in calls[0]["google_search_term"]
        assert "-stage" in calls[0]["google_search_term"]
        assert search_state == (None,)
        assert canonical.iloc[0]["best_source"] == "google"
        assert canonical.iloc[0]["best_direct_url"] == "https://company.example/apply/1"
    finally:
        store.close()


def test_google_jobspy_zero_rows_records_upstream_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(jobspy_sources, "scrape_jobs", lambda **_: pd.DataFrame())
    config = SearchConfig(
        queries=("example role",),
        locations=("Lyon, France",),
        jobspy_sites=("google",),
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        row = store.conn.execute(
            "SELECT last_success_at, last_error FROM search_state WHERE source = 'google'"
        ).fetchone()

        assert counts == {"google": 0}
        assert row[0] is None
        assert "initial cursor not found" in row[1]
    finally:
        store.close()


def test_jobspy_hours_old_override_ignores_incremental_state(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Lyon, France",),
        jobspy_sites=("indeed",),
        jobspy_hours_old_override=2160,
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        store.start_search("indeed", "example role", "Lyon, France")
        store.finish_search("indeed", "example role", "Lyon, France", 0)

        jobspy_sources.scrape_jobspy_sources(config, store)

        assert calls[0]["hours_old"] == 2160
    finally:
        store.close()


def test_glassdoor_uses_city_only_location_and_country_domain_headers(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Auvergne-Rhône-Alpes, France",),
        jobspy_sites=("glassdoor",),
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        search_state = store.conn.execute(
            "SELECT location, last_error FROM search_state WHERE source = 'glassdoor'"
        ).fetchone()

        assert counts == {"glassdoor": 0}
        assert calls[0]["location"] == "Lyon"
        assert calls[0]["results_wanted"] == config.jobspy.glassdoor_results_wanted_per_query
        assert jobspy_sources.glassdoor_headers["authority"] == "www.glassdoor.fr"
        assert jobspy_sources.glassdoor_headers["origin"] == "https://www.glassdoor.fr"
        assert jobspy_sources.glassdoor_headers["referer"] == "https://www.glassdoor.fr/"
        assert search_state[0] == "Lyon"
        assert "Glassdoor returned 0 rows" in search_state[1]
    finally:
        store.close()


def test_glassdoor_strips_france_from_city_location() -> None:
    config = SearchConfig()

    assert jobspy_sources._site_location("glassdoor", "Lyon, France", config) == "Lyon"
    assert jobspy_sources._site_location("glassdoor", "Paris", config) == "Paris"


def test_glassdoor_patch_uses_tolerant_scraper() -> None:
    jobspy_sources._patch_glassdoor_scraper()

    assert jobspy_sources.scrape_jobs.__globals__["Glassdoor"] is jobspy_sources.TolerantGlassdoor


def test_linkedin_patch_uses_tolerant_scraper() -> None:
    jobspy_sources._patch_linkedin_scraper()

    assert jobspy_sources.scrape_jobs.__globals__["LinkedIn"] is jobspy_sources.TolerantLinkedIn


def test_linkedin_date_parser_handles_new_class_and_relative_age() -> None:
    soup = BeautifulSoup(
        """
        <div class="base-search-card">
          <div class="base-search-card__metadata">
            <time class="job-search-card__listdate--new" datetime="2026-06-03">20 hours ago</time>
          </div>
        </div>
        """,
        "lxml",
    )

    assert jobspy_sources._linkedin_date_posted(soup.select_one(".base-search-card")) == date(2026, 6, 3)
    assert jobspy_sources._linkedin_relative_date("20 hours ago", now=datetime(2026, 6, 4, 15, 0)) == date(
        2026,
        6,
        3,
    )


def test_disabled_jobspy_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    called = False

    def fake_scrape_jobs(**kwargs):  # noqa: ARG001
        nonlocal called
        called = True
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Lyon, France",),
        jobspy_sites=("google",),
        enabled_sources={"google": False},
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        row = store.conn.execute("SELECT COUNT(*) FROM search_state WHERE source = 'google'").fetchone()

        assert counts == {}
        assert called is False
        assert row == (0,)
    finally:
        store.close()
