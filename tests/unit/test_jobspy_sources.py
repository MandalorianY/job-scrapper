from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import Mock

import pandas as pd
import pytest
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.config.settings import LocationScopeConfig
from scrapper.models import JobOffer
from scrapper.scrapers import jobspy_sources
from scrapper.storage import JobStore


def _jobspy_only(*sites: str) -> dict[str, bool]:
    return {site: site in sites for site in jobspy_sources.JOBSPY_SITES}


def test_google_jobspy_source_uses_google_search_term_and_records_rows(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    toulouse_scope: LocationScopeConfig,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_scrape_jobs(**kwargs: object) -> pd.DataFrame:
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
        location_scope=toulouse_scope,
        enabled_sources=_jobspy_only("google"),
        paths={"database_path": tmp_path / "jobs.duckdb"},
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
        search_term = calls[0]["google_search_term"]
        assert isinstance(search_term, str)
        assert '"Toulouse, France"' in search_term
        assert "Colomiers" not in search_term
        assert "-stage" in search_term
        assert search_state == (None,)
        assert canonical.iloc[0]["best_source"] == "google"
        assert canonical.iloc[0]["best_direct_url"] == "https://company.example/apply/1"
    finally:
        store.close()


@pytest.mark.parametrize("source_error", [None, "HTTP 429"])
def test_google_jobspy_zero_rows_records_upstream_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, source_error: str | None
) -> None:
    jobs = pd.DataFrame()
    if source_error:
        jobs.attrs["errors"] = {"google": source_error}
    monkeypatch.setattr(jobspy_sources, "scrape_jobs", lambda **_: jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        enabled_sources=_jobspy_only("google"),
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        row = store.conn.execute(
            "SELECT last_success_at, last_error FROM search_state WHERE source = 'google'"
        ).fetchone()

        assert counts == {"google": 0}
        assert row is not None
        assert row[0] is None
        assert (source_error or "initial cursor not found") in row[1]
    finally:
        store.close()


def test_jobspy_batches_compatible_sites_in_one_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_scrape_jobs(**kwargs: object) -> pd.DataFrame:
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
        enabled_sources=_jobspy_only("indeed", "linkedin", "google"),
        location_scope={"enabled": False},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)

        assert len(calls) == 1
        assert calls[0]["site_name"] == ["indeed", "linkedin", "google"]
        assert "user_agent" not in calls[0]
        assert counts == {"indeed": 1, "linkedin": 1, "google": 1}
    finally:
        store.close()


def test_hellowork_jobspy_retains_search_state_and_updates_existing_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    query, location = "example role", "Toulouse, France"
    config = SearchConfig(
        queries=(query,),
        locations=(location,),
        enabled_sources=_jobspy_only("linkedin", "hellowork"),
        distance_km=25,
        location_scope=toulouse_scope,
    )
    offer_url = "https://www.hellowork.com/fr-fr/emplois/1.html"
    listing = {
        "site": "hellowork",
        "id": "hw-1",
        "job_url": offer_url,
        "title": "Example coordinator",
        "company": "Example",
        "location": "Toulouse",
        "description": "Full-time example role.",
        "job_type": "fulltime",
    }
    internship = {**listing, "id": "hw-2", "job_url": offer_url.replace("/1.html", "/2.html"), "job_type": "internship"}
    jobs = Mock(
        side_effect=lambda **options: pd.DataFrame([listing, internship] if options["site_name"] == "hellowork" else [])
    )
    monkeypatch.setattr(jobspy_sources, "scrape_jobs", jobs)

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(JobOffer(source="hellowork", source_url=offer_url, title=listing["title"]))
        store.start_search("hellowork", query, location)
        store.finish_search("hellowork", query, location, 1)

        assert jobspy_sources.scrape_jobspy_sources(config, store) == {"linkedin": 0, "hellowork": 1}
        calls = {call.kwargs["site_name"]: call.kwargs for call in jobs.call_args_list}
        assert calls["hellowork"]["fetch_description"] is True
        assert calls["hellowork"]["distance"] == 16
        assert calls["hellowork"]["hours_old"] == config.overlap_hours + 1
        assert calls["linkedin"]["distance"] == 25
        assert calls["linkedin"]["fetch_description"] is False
        assert store.conn.execute("SELECT source, source_url, description FROM raw_offers").fetchall() == [
            ("hellowork", offer_url, listing["description"]),
        ]
    finally:
        store.close()


def test_jobspy_records_failed_batch_and_successful_retry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_scrape_jobs(**kwargs: object) -> pd.DataFrame:
        if kwargs["site_name"] == ["indeed", "linkedin"]:
            raise RuntimeError("Batch unavailable")
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example",), locations=("Toulouse, France",), enabled_sources=_jobspy_only("indeed", "linkedin")
    )
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert jobspy_sources.scrape_jobspy_sources(config, store) == {"indeed": 0, "linkedin": 0}
        assert store.conn.execute(
            "SELECT error, count(*) FROM search_attempts GROUP BY error ORDER BY error NULLS LAST"
        ).fetchall() == [("Batch unavailable", 2), (None, 2)]
        assert store.conn.execute("SELECT DISTINCT status, seen_count FROM source_health").fetchall() == [
            ("succeeded", 0)
        ]
    finally:
        store.close()


@pytest.fixture
def empty_scrape_jobs(monkeypatch: pytest.MonkeyPatch) -> Mock:
    scrape_jobs = Mock(return_value=pd.DataFrame())
    monkeypatch.setattr(jobspy_sources, "scrape_jobs", scrape_jobs)
    return scrape_jobs


def test_jobspy_hours_old_override_ignores_incremental_state(tmp_path: Path, empty_scrape_jobs: Mock) -> None:
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        enabled_sources=_jobspy_only("indeed"),
        jobspy_hours_old_override=2160,
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        store.start_search("indeed", "example role", "Toulouse, France")
        store.finish_search("indeed", "example role", "Toulouse, France", 0)

        jobspy_sources.scrape_jobspy_sources(config, store)

        assert empty_scrape_jobs.call_args_list[0].kwargs["hours_old"] == 2160
    finally:
        store.close()


def test_glassdoor_uses_city_only_location_and_configured_country(tmp_path: Path, empty_scrape_jobs: Mock) -> None:
    config = SearchConfig(
        queries=("example role",),
        locations=("Occitanie, France",),
        jobspy={"glassdoor_regional_fallback_term": "occitanie", "glassdoor_location_override": "Toulouse"},
        enabled_sources=_jobspy_only("glassdoor"),
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        counts = jobspy_sources.scrape_jobspy_sources(config, store)
        search_state = store.conn.execute(
            "SELECT location, last_error FROM search_state WHERE source = 'glassdoor'"
        ).fetchone()

        assert counts == {"glassdoor": 0}
        first_request = empty_scrape_jobs.call_args_list[0].kwargs
        assert first_request["location"] == "Toulouse"
        assert first_request["results_wanted"] == config.jobspy.glassdoor_results_wanted_per_query
        assert first_request["country_indeed"] == config.country
        assert search_state is not None
        assert search_state[0] == "Toulouse"
        assert "Glassdoor returned 0 rows" in search_state[1]
    finally:
        store.close()


def test_glassdoor_respects_smaller_general_result_limit() -> None:
    config = SearchConfig(results_wanted_per_query=1, jobspy={"glassdoor_results_wanted_per_query": 5})
    assert jobspy_sources._results_wanted("glassdoor", config) == 1


def test_glassdoor_strips_france_from_city_location() -> None:
    config = SearchConfig()

    assert jobspy_sources._site_location("glassdoor", "Toulouse, France", config) == "Toulouse"
    assert jobspy_sources._site_location("glassdoor", "Paris", config) == "Paris"


def test_linkedin_fetches_detail_without_changing_other_sites(monkeypatch: pytest.MonkeyPatch) -> None:
    scrape = Mock(return_value=[])
    monkeypatch.setattr(jobspy_sources.JobSpyLinkedIn, "scrape", scrape)
    scraper_input = jobspy_sources.ScraperInput.model_validate(
        {"site_name": [jobspy_sources.Site.LINKEDIN, jobspy_sources.Site.INDEED]}
    )

    assert jobspy_sources.TolerantLinkedIn().scrape(scraper_input) == []
    assert scrape.call_args.args[0].fetch_description
    assert not scraper_input.fetch_description


def test_linkedin_uses_guest_detail_when_listing_page_omits_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobspy_sources.JobSpyLinkedIn, "_fetch_details", lambda self, job_id: {})
    scraper = jobspy_sources.TolerantLinkedIn()
    scraper.scraper_input = jobspy_sources.ScraperInput.model_validate({"site_name": [jobspy_sources.Site.LINKEDIN]})
    scraper.session = Mock()
    scraper.session.get.return_value = Mock(
        status_code=200,
        text="""
        <div class="show-more-less-html__markup"><p>Définir les campagnes.</p></div>
        <h3 class="description__job-criteria-subheader">Type d’emploi</h3>
        <span class="description__job-criteria-text">Temps plein</span>
    """,
    )

    detail = scraper._fetch_details("123")

    description = detail["description"]
    assert isinstance(description, str)
    assert "Définir les campagnes." in description
    assert detail["job_type"] == [jobspy_sources.JobType.FULL_TIME]
    scraper.session.get.assert_called_once_with("https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/123")


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

    job_card = soup.select_one(".base-search-card")
    assert job_card is not None
    assert jobspy_sources._linkedin_date_posted(job_card) == date(2026, 6, 3)
    assert jobspy_sources._linkedin_relative_date("20 hours ago", now=datetime(2026, 6, 4, 15, 0, tzinfo=UTC)) == date(
        2026,
        6,
        3,
    )


def test_indeed_expired_message_detector() -> None:
    assert jobspy_sources._indeed_html_is_expired("<main>Cette offre a expiré sur Indeed</main>")
    assert not jobspy_sources._indeed_html_is_expired("<main>Cette offre est disponible</main>")


def test_disabled_jobspy_source_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    called = False

    def fake_scrape_jobs(**kwargs: object) -> pd.DataFrame:
        nonlocal called
        called = True
        return pd.DataFrame()

    monkeypatch.setattr(jobspy_sources, "scrape_jobs", fake_scrape_jobs)
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        enabled_sources=_jobspy_only(),
        paths={"database_path": tmp_path / "jobs.duckdb"},
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
