from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

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
        assert "Toulouse" in calls[0]["google_search_term"]
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
        assert "initial cursor not found" in row[1]
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
        assert calls[0]["linkedin_fetch_description"] is True
        assert counts == {"indeed": 1, "linkedin": 1, "google": 1}
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
        assert calls[0]["location"] == "Toulouse"
        assert calls[0]["results_wanted"] == config.jobspy.glassdoor_results_wanted_per_query
        assert jobspy_sources.glassdoor_headers["authority"] == "www.glassdoor.fr"
        assert jobspy_sources.glassdoor_headers["origin"] == "https://www.glassdoor.fr"
        assert jobspy_sources.glassdoor_headers["referer"] == "https://www.glassdoor.fr/"
        assert search_state[0] == "Toulouse"
        assert "Glassdoor returned 0 rows" in search_state[1]
    finally:
        store.close()


def test_glassdoor_strips_france_from_city_location() -> None:
    config = SearchConfig()

    assert jobspy_sources._site_location("glassdoor", "Toulouse, France", config) == "Toulouse"
    assert jobspy_sources._site_location("glassdoor", "Paris", config) == "Paris"


def test_glassdoor_patch_uses_tolerant_scraper() -> None:
    jobspy_sources._patch_glassdoor_scraper()

    assert jobspy_sources.scrape_jobs.__globals__["Glassdoor"] is jobspy_sources.TolerantGlassdoor


@pytest.mark.parametrize("html", ['{"token":"test-token"}', r'{\"token\":\"test-token\"}'])
def test_glassdoor_initializes_session_from_homepage(html: str) -> None:
    scraper = jobspy_sources.TolerantGlassdoor(user_agent="configured-agent")
    scraper.base_url = "https://www.glassdoor.fr/"
    scraper.session = Mock(headers={})
    scraper.session.get.return_value = SimpleNamespace(status_code=200, text=html)

    assert scraper._get_csrf_token() == "test-token"
    assert scraper.base_url == "https://www.glassdoor.fr"
    assert scraper.session.headers["user-agent"] == "configured-agent"
    scraper.session.get.assert_called_once_with(
        "https://www.glassdoor.fr/", timeout_seconds=15, allow_redirects=True
    )


@pytest.mark.parametrize(
    ("status", "html", "error"),
    [(403, "Forbidden", "HTTP 403"), (200, "<html></html>", "CSRF token")],
)
def test_glassdoor_session_failure_is_explicit(status: int, html: str, error: str) -> None:
    scraper = jobspy_sources.TolerantGlassdoor()
    scraper.base_url = "https://www.glassdoor.fr/"
    scraper.session = Mock(headers={})
    scraper.session.get.return_value = SimpleNamespace(status_code=status, text=html)

    with pytest.raises(jobspy_sources.GlassdoorException, match=error):
        scraper._get_csrf_token()


@pytest.mark.parametrize(("code", "kind"), [("C", "CITY"), ("S", "STATE"), ("N", "COUNTRY")])
def test_glassdoor_uses_current_location_autocomplete(code: str, kind: str) -> None:
    scraper = jobspy_sources.TolerantGlassdoor()
    scraper.base_url = "https://www.glassdoor.fr"
    scraper.session = Mock()
    scraper.session.get.return_value = Mock(status_code=200)
    scraper.session.get.return_value.json.return_value = [{"locationId": 2864777, "locationType": code}]
    location = "Saint-Étienne, France & alentours"

    assert scraper._get_location(location, False) == (2864777, kind)
    request_url = urlparse(scraper.session.get.call_args.args[0])
    assert request_url.path == "/autocomplete/location"
    assert parse_qs(request_url.query) == {
        "locationTypeFilters": ["CITY,STATE,COUNTRY"], "caller": ["jobs"], "term": [location]
    }


@pytest.mark.parametrize(
    ("status", "matches", "error"),
    [
        (404, [], "HTTP 404"),
        (200, [], "no matches"),
        (200, [{"locationId": 1, "locationType": "unknown"}], "unsupported location type"),
    ],
)
def test_glassdoor_location_errors_are_explicit(status: int, matches: list, error: str) -> None:
    scraper = jobspy_sources.TolerantGlassdoor()
    scraper.base_url = "https://www.glassdoor.fr"
    scraper.session = Mock()
    scraper.session.get.return_value = Mock(status_code=status)
    scraper.session.get.return_value.json.return_value = matches

    with pytest.raises(jobspy_sources.GlassdoorException, match=error):
        scraper._get_location("Toulouse", False)


@pytest.mark.parametrize("base_url", ["https://www.glassdoor.fr", "https://www.glassdoor.fr/"])
def test_glassdoor_job_links_preserve_path_separator(base_url: str) -> None:
    scraper = jobspy_sources.TolerantGlassdoor()
    scraper.base_url = base_url
    offer = scraper._process_job({"jobview": {
        "job": {"listingId": "123", "jobTitleText": "Chargé de communication"},
        "header": {"employerNameFromSearch": "Example", "employer": {"id": 456}},
    }})

    assert offer.job_url == "https://www.glassdoor.fr/job-listing/j?jl=123"
    assert offer.company_url == "https://www.glassdoor.fr/Overview/W-EI_IE456.htm"
    assert offer.date_posted is None


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
