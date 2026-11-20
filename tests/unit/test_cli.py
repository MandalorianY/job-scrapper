import asyncio
import json
from importlib import import_module
from pathlib import Path
from typing import Never

import httpx
import pytest
from typer.testing import CliRunner

from scrapper import cli, search_regions
from scrapper.config import SearchConfig
from scrapper.config.settings import PACKAGE_CONFIG_DIR
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.scrapers.aggregators import site_search_templates
from scrapper.storage import JobStore


def test_show_config_reports_effective_values_and_origins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        'queries: [Linux]\nlocations: ["Lille, France"]\ndistance_km: 10\n'
        "enabled_sources:\n  welcome_to_the_jungle: false\n"
        "web_search:\n  location_terms: [lille]\n",
        encoding="utf-8",
    )
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "SCRAPPER_APP_ENV=prod\nSCRAPPER_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY=private-key\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(import_module("scrapper.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("SCRAPPER_DISABLE_LOCAL_CONFIG")
    monkeypatch.delenv("SCRAPPER_APP_ENV", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.setenv("SCRAPPER_EXTRA_HEADERS", '{"Authorization": "private-header"}')
    monkeypatch.setenv("SCRAPPER_RESULTS_WANTED_PER_QUERY", "30")
    monkeypatch.setenv("SCRAPPER_WEB_SEARCH_MAX_DETAIL_PAGES", "25")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Printing configuration must have no run side effects")

    for name in ("configure_logging", "_tracker_sync_hooks", "_job_store"):
        monkeypatch.setattr(cli, name, forbidden)

    result = CliRunner().invoke(cli.app, ["--show-config", "--query", "Administrateur Système", "--db", "test.duckdb"])
    assert result.exit_code == 0, result.output
    assert "private-key" not in result.output
    assert "private-header" not in result.output
    report = json.loads(result.stdout)
    values, sources = report["values"], report["sources"]
    assert values["app_env"] == "prod"
    assert values["queries"] == ["Administrateur Système"]
    assert values["locations"] == ["Lille, France"]
    assert values["distance_km"] == 10
    assert values["enabled_sources"]["welcome_to_the_jungle"] is False
    assert values["enabled_sources"]["hellowork"] is True
    assert values["results_wanted_per_query"] == 30
    assert values["custom_max_pages"] == 3
    assert values["aggregators"]["location_terms"] == ["lille"]
    assert values["aggregators"]["max_detail_pages"] == 25
    assert values["paths"]["database_path"] == str(tmp_path / "test.duckdb")
    assert sources["app_env"] == str(dotenv)
    assert sources["queries"] == sources["paths.database_path"] == "command line"
    assert sources["locations"] == sources["enabled_sources.welcome_to_the_jungle"] == str(local_config)
    assert sources["enabled_sources.hellowork"] == str(PACKAGE_CONFIG_DIR / "config.yaml")
    assert sources["custom_max_pages"] == str(PACKAGE_CONFIG_DIR / "config.prod.yaml")
    assert sources["results_wanted_per_query"] == "environment"
    assert sources["aggregators.location_terms"] == str(local_config)
    assert sources["aggregators.max_detail_pages"] == "environment"
    assert sources["jobspy_hours_old_override"] == "default"
    assert sources["welcome_to_the_jungle.algolia_api_key"] == str(dotenv)


def test_status_previews_search_configuration_without_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        "include_terms: [data analyst]\nexclude_terms: [volunteer]\n"
        "excluded_title_patterns: [senior]\nexcluded_companies_by_source:\n  linkedin: [Example Company]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(import_module("scrapper.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("SCRAPPER_DISABLE_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("SCRAPPER_DISTANCE_KM", "10")
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__GOOGLE", "false")
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__AGGREGATORS", "false")
    monkeypatch.setenv("SCRAPPER_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY", "private-key")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Source status must have no run side effects")

    for name in ("configure_logging", "_tracker_sync_hooks", "_job_store"):
        monkeypatch.setattr(cli, name, forbidden)

    def resolve(config: SearchConfig) -> SearchConfig:
        aggregators = config.aggregators.model_copy(update={"direct_locations": ("32R",)})
        return config.model_copy(update={"aggregators": aggregators})

    monkeypatch.setattr(cli, "route_location_overrides", resolve)

    runner = CliRunner()
    for flags, jobspy_status, custom_status in (
        ([], "Enabled", "Enabled"),
        (["--skip-jobspy"], "Disabled", "Enabled"),
        (["--skip-custom"], "Enabled", "Disabled"),
        (["--skip-jobspy", "--skip-custom"], "Disabled", "Disabled"),
        (["--export-only"], "Disabled", "Disabled"),
    ):
        result = runner.invoke(cli.app, ["--status", "--query", "analytics", "--location", "Lille, France", *flags])
        assert result.exit_code == 0, result.output
        for value in ("Lille, France", "10 km", "analytics", "data analyst", "volunteer", "senior", "Example Company"):
            assert value in result.stdout
        assert "config.local.yaml: exclude_terms" in result.stdout
        assert "environment: distance_km, enabled_sources" in result.stdout
        assert "command line: location_label, locations, queries" in result.stdout
        assert "private-key" not in result.stdout
        rows = [
            tuple(cell.strip() for cell in line.strip("│").split("│"))
            for line in result.stdout.splitlines()
            if line.startswith("│")
        ]
        assert ("indeed", "JobSpy", jobspy_status) in rows
        assert ("google", "JobSpy", "Disabled") in rows
        assert ("hellowork", "JobSpy", jobspy_status) in rows
        assert ("welcome_to_the_jungle", "Custom", custom_status) in rows
        assert all(row[0] != "web_search" for row in rows)
        assert ("emploi_territorial", "Aggregator", "Disabled") in rows
        assert ("cadremploi", "Aggregator", "Disabled") in rows
        if not (jobspy_status == custom_status == "Disabled"):
            assert "32R" in result.stdout
            assert "84R" not in result.stdout


def test_location_override_routes_each_provider_and_admits_nearby_municipalities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/regions/11":
            return httpx.Response(200, json={"nom": "Île-de-France"})
        if request.url.params.get("codeRegion") == "11":
            return httpx.Response(
                200,
                json=[
                    {
                        "nom": "Issy-les-Moulineaux",
                        "centre": {"coordinates": [2.274, 48.824]},
                    }
                ],
            )
        assert request.url.params.get("nom") == "Paris"
        return httpx.Response(
            200,
            json=[
                {
                    "nom": "Paris",
                    "codeRegion": "11",
                    "codeDepartement": "75",
                    "centre": {"coordinates": [2.347, 48.8589]},
                }
            ],
        )

    client_class = httpx.Client
    monkeypatch.setattr(
        search_regions.httpx,
        "Client",
        lambda **kwargs: client_class(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    original = SearchConfig(locations=("Paris, France",), jobspy={"google_query_places": ["custom place"]})
    direct_specs = tuple(
        spec.model_copy(update={"search_url_templates": (*spec.search_url_templates, "https://custom.example/{query}")})
        if spec.source == "meteojob"
        else spec
        for spec in original.aggregators.direct_specs
    )
    original = original.model_copy(
        update={"aggregators": original.aggregators.model_copy(update={"direct_specs": direct_specs})}
    )
    config = search_regions.route_location_overrides(original)

    assert config.aggregators.direct_locations == ("11R",)
    assert config.jobspy.google_query_places == ("custom place",)
    assert [center.name for center in config.location_scope.centers] == ["Paris"]
    assert location_is_in_scope("Issy-les-Moulineaux", config.location_scope)
    assert not location_is_in_scope("Toulouse", config.location_scope)
    direct = {spec.source: spec.search_url_templates for spec in config.aggregators.direct_specs}
    site = {
        spec.source: site_search_templates(spec, "Paris, France", config)
        for spec in config.aggregators.site_search_sources
    }
    assert direct["meteojob"] == (
        "https://www.meteojob.com/jobsearch/offers?what={query}&where=Paris",
        "https://custom.example/{query}",
    )
    assert direct["emploi_territorial"][0].endswith("&search-region=11")
    assert direct["jobijoba"][0].endswith("/{query}/Paris")
    assert "/fr/s/jobs/Paris--France/" in direct["makesense"][0]
    assert site["cadremploi"][0].endswith("&ville=paris-75")
    assert site["isarta"][0].endswith("?cat=ile-de-france&query={query}")
    assert site["apec"][0].endswith("?q={query}&l=paris-75&page=0")

    preview = CliRunner().invoke(cli.app, ["--show-config", "--location", "Paris, France"])
    assert preview.exit_code == 0, preview.output
    report = json.loads(preview.stdout)
    assert report["values"]["aggregators"]["direct_locations"] == ["11R"]
    assert report["sources"]["aggregators.direct_locations"] == "resolved from --location"


def test_custom_sites_controls_dispatch_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def welcome_to_the_jungle(config: SearchConfig, store: JobStore) -> int:
        return 3

    async def unexpected(*args: object) -> Never:
        raise AssertionError("An unselected custom scraper ran")

    monkeypatch.setattr(cli, "scrape_welcome_to_the_jungle", welcome_to_the_jungle)
    monkeypatch.setattr(cli, "scrape_aggregators", unexpected)
    monkeypatch.setenv("SCRAPPER_CUSTOM_SITES", '["welcome_to_the_jungle"]')
    config = SearchConfig(database_path=tmp_path / "jobs.duckdb")
    store = JobStore(config.database_path)
    try:
        assert asyncio.run(cli._run_custom_sources(config, store)) == ({"welcome_to_the_jungle": 3}, {})
    finally:
        store.close()

    status = CliRunner().invoke(cli.app, ["--status"])
    assert status.exit_code == 0, status.output
    sources = {
        row[0]: row[-1]
        for line in status.stdout.splitlines()
        if line.startswith("│")
        for row in [tuple(cell.strip() for cell in line.strip("│").split("│"))]
    }
    assert sources["welcome_to_the_jungle"] == "Enabled"
    assert sources["cadremploi"] == "Disabled"


def test_local_export_survives_source_failure_and_never_syncs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def sources(config: SearchConfig, store: JobStore) -> tuple[dict[str, int], dict[str, str]]:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse",
            )
        )
        return {"example": 1}, {"unavailable_board": "Temporary source outage"}

    def forbidden_sync() -> Never:
        raise AssertionError("Local validation must not fetch or publish the remote tracker")

    monkeypatch.setattr(cli, "_run_custom_sources", sources)
    monkeypatch.setattr(cli, "_tracker_sync_hooks", forbidden_sync)
    result = CliRunner().invoke(
        cli.app,
        [
            "--skip-jobspy",
            "--no-tracker-sync",
            "--db",
            str(tmp_path / "jobs.duckdb"),
            "--export-dir",
            str(tmp_path / "exports"),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "unavailable_board failed" in result.output
    assert (tmp_path / "exports/jobs.xlsx").exists()


def test_export_only_regenerates_local_files_from_duckdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "jobs.duckdb"
    export_dir = tmp_path / "exports"
    store = JobStore(database)
    try:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse, France",
            )
        )
    finally:
        store.close()

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Export-only must skip scraping, tracker sync, and observation pruning")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources", "_tracker_sync_hooks"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(JobStore, "prune_observations", forbidden)

    result = CliRunner().invoke(
        cli.app,
        [
            "--export-only",
            "--db",
            str(database),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (export_dir / "jobs.xlsx").is_file()
    for name in ("jobs.csv", "jobs_raw.csv"):
        assert "Example coordinator" in (export_dir / name).read_text(encoding="utf-8")


def test_offline_replay_restores_purged_offers_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive_path = tmp_path / "archive.duckdb"
    archive = JobStore(archive_path)
    try:
        offer = JobOffer(
            source="example",
            source_id="1",
            source_url="https://example.com/jobs/1",
            title="Example coordinator",
            company="Example",
            location="Toulouse, France",
            scraped_at="2026-06-04T12:00:00+00:00",
            raw={"listing_id": "1"},
        )
        archive.upsert_offer(offer)
        archive.upsert_offer(offer)
        assert archive.purge_nonmatching_offers(("unrelated",)) == 1
        assert archive.conn.execute("SELECT count(*) FROM offer_observations").fetchone() == (1,)
    finally:
        archive.close()

    def forbidden(*args: object) -> Never:
        raise AssertionError("Replay must never scrape or use the remote tracker")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources", "_tracker_sync_hooks"):
        monkeypatch.setattr(cli, name, forbidden)

    target = tmp_path / "rebuilt.duckdb"
    for _ in range(2):
        result = CliRunner().invoke(
            cli.app,
            [
                "--replay-from",
                str(archive_path),
                "--db",
                str(target),
                "--export-dir",
                str(tmp_path / "exports"),
                "--log-file",
                str(tmp_path / "run.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "observations_replayed" in result.output
        store = JobStore(target)
        try:
            assert len(store.raw_dataframe()) == len(store.canonical_dataframe()) == 1
            assert store.conn.execute("SELECT count(*) FROM search_state").fetchone() == (0,)
        finally:
            store.close()
