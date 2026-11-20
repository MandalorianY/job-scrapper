from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.scrapers import jobspy_sources
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
                    "location": "Toulouse",
                    "description": "Full-time example role.",
                    "date_posted": "2026-05-31",
                    "job_type": "CDI",
                }
            ]
        )

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        jobspy_sites=("google",),
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    progress_messages: list[str] = []
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store, progress=progress_messages.append)
        search_state = store.conn.execute("SELECT last_error FROM search_state WHERE source = 'google'").fetchone()
        canonical = store.canonical_dataframe()

        assert counts == {"google": 1}
        assert progress_messages == [
            "JobSpy start [1/1]: google | example role | Toulouse, France (hours_old=336, results_wanted=25)",
            "JobSpy done [1/1]: google | example role | Toulouse, France (seen=1, stored=1)",
        ]
        assert calls[0]["site_name"] == "google"
        assert calls[0]["location"] == "Toulouse, France"
        assert '"Toulouse, France"' in calls[0]["google_search_term"]
        assert "Colomiers" not in calls[0]["google_search_term"]
        assert "-stage" in calls[0]["google_search_term"]
        assert search_state == (None,)
        assert canonical.iloc[0]["best_source"] == "google"
        assert canonical.iloc[0]["best_direct_url"] == "https://company.example/apply/1"
    finally:
        store.close()


@pytest.mark.parametrize("source_error", [None, "HTTP 429"])
def test_google_jobspy_zero_rows_records_upstream_error(monkeypatch, tmp_path: Path, source_error: str | None) -> None:
    jobs = pd.DataFrame()
    if source_error:
        jobs.attrs["errors"] = {"google": source_error}
    monkeypatch.setattr(jobspy_sources, "scrape_jobs", lambda **_: jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
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
        assert (source_error or "initial cursor not found") in row[1]
    finally:
        store.close()


def test_jobspy_batches_compatible_sites_in_one_call(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_scrape_jobs(**kwargs):
        calls.append(kwargs)
        return pd.DataFrame(
            [
                {
                    "site": "indeed",
                    "id": "in-1",
                    "job_url": "https://company.example/indeed/1",
                    "title": "Example coordinator",
                    "company": "Example",
                    "location": "Toulouse",
                    "description": "Full-time example role.",
                    "date_posted": "2026-05-31",
                    "job_type": "CDI",
                },
                {
                    "site": "linkedin",
                    "id": "li-1",
                    "job_url": "https://linkedin.example/jobs/1",
                    "title": "Example coordinator",
                    "company": "Example",
                    "location": "Toulouse",
                    "description": "Full-time example role.",
                    "date_posted": "2026-05-31",
                    "job_type": "CDI",
                },
                {
                    "site": "google",
                    "id": "go-1",
                    "job_url": "https://google.example/jobs/1",
                    "title": "Example coordinator",
                    "company": "Example",
                    "location": "Toulouse",
                    "description": "Full-time example role.",
                    "date_posted": "2026-05-31",
                    "job_type": "CDI",
                },
            ]
        )

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        jobspy_sites=("indeed", "linkedin", "google"),
        location_scope={"enabled": False},
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)

        assert len(calls) == 1
        assert calls[0]["site_name"] == ["indeed", "linkedin", "google"]
        assert counts == {"indeed": 1, "linkedin": 1, "google": 1}
    finally:
        store.close()


def test_jobspy_records_failed_batch_and_successful_retry(monkeypatch, tmp_path: Path) -> None:
    def fake_scrape_jobs(**kwargs):
        if kwargs["site_name"] == ["indeed", "linkedin"]:
            raise RuntimeError("Batch unavailable")
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(queries=("example",), locations=("Toulouse, France",), jobspy_sites=("indeed", "linkedin"))
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert jobspy_sources.scrape_jobspy_sources(config, store) == {"indeed": 0, "linkedin": 0}
        assert store.conn.execute(
            "SELECT error, count(*) FROM search_attempts GROUP BY error ORDER BY error NULLS LAST"
        ).fetchall() == [("Batch unavailable", 2), (None, 2)]
        assert store.conn.execute("SELECT DISTINCT status, seen_count FROM source_health").fetchall() == [("succeeded", 0)]
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
        locations=("Toulouse, France",),
        jobspy_sites=("indeed",),
        jobspy_hours_old_override=2160,
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        store.start_search("indeed", "example role", "Toulouse, France")
        store.finish_search("indeed", "example role", "Toulouse, France", 0)

        jobspy_sources.scrape_jobspy_sources(config, store)

        assert calls[0]["hours_old"] == 2160
    finally:
        store.close()


def test_glassdoor_uses_city_only_location_and_configured_country(monkeypatch, tmp_path: Path) -> None:
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
        assert calls[0]["location"] == "Toulouse"
        assert calls[0]["results_wanted"] == config.jobspy.glassdoor_results_wanted_per_query
        assert calls[0]["country_indeed"] == config.country
        assert search_state[0] == "Toulouse"
        assert "Glassdoor returned 0 rows" in search_state[1]
    finally:
        store.close()


def test_glassdoor_strips_france_from_city_location() -> None:
    config = SearchConfig()

    assert jobspy_sources._site_location("glassdoor", "Toulouse, France", config) == "Toulouse"
    assert jobspy_sources._site_location("glassdoor", "Paris", config) == "Paris"


def test_linkedin_fetches_detail_without_changing_other_sites(monkeypatch) -> None:
    scrape = Mock(return_value=[])
    monkeypatch.setattr(jobspy_sources.JobSpyLinkedIn, "scrape", scrape)
    scraper_input = jobspy_sources.ScraperInput(site_name=[jobspy_sources.Site.LINKEDIN, jobspy_sources.Site.INDEED])

    assert jobspy_sources.TolerantLinkedIn().scrape(scraper_input) == []
    assert scrape.call_args.args[0].fetch_description
    assert not scraper_input.fetch_description


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
    assert jobspy_sources._linkedin_relative_date("20 hours ago", now=datetime(2026, 6, 4, 15, 0, tzinfo=UTC)) == date(
        2026,
        6,
        3,
    )


def test_indeed_expired_message_detector() -> None:
    assert jobspy_sources._indeed_html_is_expired("<main>Cette offre a expiré sur Indeed</main>")
    assert not jobspy_sources._indeed_html_is_expired("<main>Cette offre est disponible</main>")


def test_disabled_jobspy_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    called = False

    def fake_scrape_jobs(**kwargs):
        nonlocal called
        called = True
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
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
