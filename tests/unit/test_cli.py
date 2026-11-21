import asyncio
import json
from importlib import import_module
from pathlib import Path
from typing import Never

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from scrapper import cli, search_regions
from scrapper.config import SearchConfig
from scrapper.config.settings import PACKAGE_CONFIG_DIR, LocationScopeConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.scrapers.aggregators import site_search_templates
from scrapper.scrapers.async_http import SourceBlockedError
from scrapper.storage import JobStore


def test_search_configuration_rejects_handwritten_coordinates() -> None:
    with pytest.raises(ValidationError, match="The scraper resolves coordinates automatically"):
        SearchConfig(location_scope={"city_coordinates": {"Paris": [48.8589, 2.347]}})


def test_show_config_reports_effective_values_and_origins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        'queries: [Linux]\nlocations: ["Lille, France"]\ndistance_km: 10\n'
        "enabled_sources:\n  welcome_to_the_jungle: false\n"
        "aggregators:\n  location_terms: [lille]\n"
        "  direct_specs:\n    makesense:\n      search_url_templates: ['https://example.test/jobs?q={query}']\n"
        "  site_search_sources:\n    cadremploi:\n      query_template: 'existing key {location} {query}'\n",
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
    monkeypatch.setenv("SCRAPPER_EXTRA_HEADERS", '{"Authorization": "private-header"}')
    monkeypatch.setenv("SCRAPPER_RESULTS_WANTED_PER_QUERY", "30")
    monkeypatch.setenv("SCRAPPER_AGGREGATORS__MAX_DETAIL_PAGES", "25")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Printing configuration must have no run side effects")

    for name in ("configure_logging", "_job_store"):
        monkeypatch.setattr(cli, name, forbidden)

    result = CliRunner().invoke(
        cli.app, ["--show-config", "--export-only", "--query", "Administrateur Système", "--db", "test.duckdb"]
    )
    assert result.exit_code == 0, result.output
    assert "private-key" not in result.output
    assert "private-header" not in result.output
    report = json.loads(result.stdout)
    values, sources = report["values"], report["sources"]
    assert values["app_env"] == "prod"
    assert values["queries"] == ["Administrateur Système"]
    assert values["locations"] == ["Lille, France"]
    assert values["distance_km"] == 10
    assert "jobspy_sites" not in values
    assert "custom_sites" not in values
    assert values["enabled_sources"]["welcome_to_the_jungle"] is False
    assert "extra_headers" not in values
    assert values["enabled_sources"]["apec"] is False
    assert values["results_wanted_per_query"] == 30
    assert values["custom_max_pages"] == 3
    assert values["aggregators"]["location_terms"] == ["lille"]
    assert values["aggregators"]["max_detail_pages"] == 25
    direct_specs = {spec["source"]: spec for spec in values["aggregators"]["direct_specs"]}
    assert "france_travail" in direct_specs
    assert direct_specs["makesense"]["search_url_templates"] == ["https://example.test/jobs?q={query}"]
    site_specs = {spec["source"]: spec for spec in values["aggregators"]["site_search_sources"]}
    assert "isarta" in site_specs
    assert site_specs["cadremploi"]["query_template"] == "existing key {location} {query}"
    assert values["paths"]["database_path"] == str(tmp_path / "test.duckdb")
    assert sources["app_env"] == str(dotenv)
    assert sources["queries"] == sources["paths.database_path"] == "command line"
    assert sources["locations"] == sources["enabled_sources.welcome_to_the_jungle"] == str(local_config)
    assert sources["enabled_sources.apec"] == str(PACKAGE_CONFIG_DIR / "config.yaml")
    assert sources["custom_max_pages"] == str(PACKAGE_CONFIG_DIR / "config.prod.yaml")
    assert sources["results_wanted_per_query"] == "environment"
    assert sources["aggregators.location_terms"] == str(local_config)
    assert sources["aggregators.direct_specs"] == sources["aggregators.site_search_sources"] == str(local_config)
    assert sources["aggregators.max_detail_pages"] == "environment"
    assert sources["jobspy_hours_old_override"] == "default"
    assert sources["welcome_to_the_jungle.algolia_api_key"] == str(dotenv)


def test_status_previews_search_configuration_without_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        "include_terms: [data analyst]\nexclude_terms: [volunteer]\n"
        "excluded_title_patterns: [senior]\nexcluded_companies_by_source:\n  linkedin: [Example Company]\n"
        "aggregators:\n  max_detail_pages: 42\n  direct_locations: [32R]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(import_module("scrapper.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("SCRAPPER_DISABLE_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("SCRAPPER_DISTANCE_KM", "10")
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__GOOGLE", "false")
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__AGGREGATORS", "false")
    monkeypatch.setenv("SCRAPPER_PATHS__DATABASE_PATH", str(tmp_path / "jobs.duckdb"))
    monkeypatch.setenv("SCRAPPER_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY", "private-key")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Status must be offline and have no run side effects")

    for name in ("configure_logging", "_job_store", "route_location_overrides"):
        monkeypatch.setattr(cli, name, forbidden)

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
        assert "Search profile config.local.yaml (overrides applied)" in " ".join(result.stdout.split())
        assert "config.local.yaml: aggregators, exclude_terms" in result.stdout
        assert "environment: distance_km, enabled_sources" in result.stdout
        assert "command line: location_label, locations, queries" in result.stdout
        assert "42 aggregator detail pages" in " ".join(result.stdout.split())
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
        assert ("emploi_territorial", "Aggregator", "Disabled") in rows
        assert ("cadremploi", "Aggregator", "Disabled") in rows
        assert "32R" in result.stdout

    profile = tmp_path / "config.yaml"
    profile.write_text("queries: [graphiste]\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["--status", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    assert "Search profile config.yaml (overrides applied)" in " ".join(result.stdout.split())
    assert "config.yaml: queries" in result.stdout
    assert "config.local.yaml:" not in result.stdout

    monkeypatch.setenv("SCRAPPER_DISABLE_LOCAL_CONFIG", "1")
    result = runner.invoke(cli.app, ["--status"])
    assert result.exit_code == 0, result.output
    assert "Search profile None" in " ".join(result.stdout.split())
    assert "Create or edit src/scrapper/config/config.local.yaml" in result.stdout
    assert "Use --profile PATH to load a different YAML file." in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("settings_json", "indicator"),
    [
        (None, "✕ Disabled"),
        ('{"enabled": false}', "✕ Disabled"),
        ('{"enabled": true, "target": "private-remote:jobs.xlsx"}', "✓ Enabled"),
        ("invalid json", "⚠ Unavailable"),
    ],
)
def test_status_shows_tracker_sync_for_selected_database(
    tmp_path: Path, settings_json: str | None, indicator: str
) -> None:
    database = tmp_path / "jobs.duckdb"
    settings_path = tmp_path / ".tracker-sync" / "settings.json"
    if settings_json is not None:
        settings_path.parent.mkdir()
        settings_path.write_text(settings_json, encoding="utf-8")

    result = CliRunner().invoke(cli.app, ["--status", "--export-only", "--db", str(database)])

    assert result.exit_code == 0, result.output
    assert f"Tracker sync  {indicator}" in result.stdout
    assert "private-remote" not in result.stdout
    assert not database.exists()


def test_location_override_routes_each_provider_and_admits_nearby_municipalities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        city = request.url.params.get("nom")
        region = request.url.params.get("codeRegion")
        requests.append((request.url.path, city or region))
        if request.url.path == "/regions/11":
            return httpx.Response(200, json={"nom": "Île-de-France"})
        if region == "11":
            return httpx.Response(
                200,
                json=[
                    {
                        "nom": "Issy-les-Moulineaux",
                        "centre": {"coordinates": [5.0, 48.824]},
                    },
                    {
                        "nom": "Issy-les-Moulineaux",
                        "centre": {"coordinates": [2.274, 48.824]},
                    },
                ],
            )
        assert city in {"Paris", "Versailles"}
        department, coordinates = ("75", [2.347, 48.8589]) if city == "Paris" else ("78", [2.130, 48.801])
        return httpx.Response(
            200,
            json=[
                {
                    "nom": city,
                    "codeRegion": "11",
                    "codeDepartement": department,
                    "centre": {"coordinates": coordinates},
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
    original = SearchConfig(
        locations=("Paris, France",),
        location_scope={"city_aliases": {"Paris": ["Grand Paris"], "Issy-les-Moulineaux": ["Issy"]}},
        jobspy={"google_query_places": ["custom place"]},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    direct_specs = tuple(
        spec.model_copy(update={"search_url_templates": (*spec.search_url_templates, "https://custom.example/{query}")})
        if spec.source == "meteojob"
        else spec
        for spec in original.aggregators.direct_specs
    )
    original = original.model_copy(
        update={"aggregators": original.aggregators.model_copy(update={"direct_specs": direct_specs})}
    )
    search_regions.route_location_overrides(original, write_cache=False)
    assert not list(tmp_path.iterdir())
    requests.clear()
    config = search_regions.route_location_overrides(original)

    assert requests == [("/communes", "Paris"), ("/regions/11", None), ("/communes", "11")]
    requests.clear()
    assert search_regions.route_location_overrides(original) == config

    smaller_scope = original.location_scope.model_copy(update={"radius_km": 2, "city_aliases": {"Paris": ("Lutèce",)}})
    smaller_config = search_regions.route_location_overrides(
        original.model_copy(update={"location_scope": smaller_scope})
    )
    assert location_is_in_scope("Lutèce", smaller_config.location_scope)
    assert not location_is_in_scope("Issy-les-Moulineaux", smaller_config.location_scope)
    assert requests == []

    expanded_locations = ("Versailles, France", *original.locations)
    expanded = search_regions.route_location_overrides(original.model_copy(update={"locations": expanded_locations}))
    assert expanded.aggregators.site_locations["Versailles, France"].department == "78"
    assert requests == [("/communes", "Versailles")]
    requests.clear()
    assert search_regions.route_location_overrides(original) == config
    assert requests == []

    assert config.aggregators.direct_locations == ("11R",)
    assert config.jobspy.google_query_places == ("custom place",)
    assert [center.name for center in config.location_scope.centers] == ["Paris"]
    assert config.location_scope.centers[0].latitude == 48.8589
    assert config.location_scope.centers[0].longitude == 2.347
    assert location_is_in_scope("Issy-les-Moulineaux", config.location_scope)
    assert config.location_scope.city_coordinates["Grand Paris"] == (48.8589, 2.347)
    assert location_is_in_scope("Issy", config.location_scope)
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

    preview_args = ["--show-config", "--db", str(original.database_path)]
    preview = CliRunner().invoke(cli.app, [*preview_args, "--location", "Paris, France"])
    assert preview.exit_code == 0, preview.output
    report = json.loads(preview.stdout)
    assert report["values"]["aggregators"]["direct_locations"] == ["11R"]
    assert report["sources"]["aggregators.direct_locations"] == "resolved from --location"

    profile = tmp_path / "designer.yaml"
    profile.write_text(
        'locations: ["Paris, France"]\nqueries: [graphiste]\ninclude_terms: [graphiste]\ndistance_km: 15\n',
        encoding="utf-8",
    )
    local = tmp_path / "config.local.yaml"
    local.write_text(
        'locations: ["Toulouse, France"]\nqueries: [marketing]\njobspy:\n  google_query_places: [Toulouse]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("SCRAPPER_DISABLE_LOCAL_CONFIG")
    monkeypatch.setattr(import_module("scrapper.config.settings"), "LOCAL_CONFIG_FILE", local)

    for args, origin in (
        (["--profile", str(profile)], "resolved from locations"),
        (["--location", "Paris, France"], "resolved from --location"),
    ):
        preview = CliRunner().invoke(cli.app, [*preview_args, *args])
        assert preview.exit_code == 0, preview.output
        report = json.loads(preview.stdout)
        assert report["values"]["jobspy"]["google_query_places"] == []
        assert report["values"]["aggregators"]["direct_locations"] == ["11R"]
        assert report["sources"]["aggregators.direct_locations"] == origin
        if origin == "resolved from locations":
            assert report["values"]["queries"] == ["graphiste"]
            assert report["values"]["location_scope"]["radius_km"] == 15
            assert report["sources"]["queries"] == str(profile)
    assert requests == []


def test_offline_export_reuses_geographic_scope_from_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    def resolve(config: SearchConfig, *, write_cache: bool) -> SearchConfig:
        assert not config.location_scope.centers
        assert write_cache
        return config.model_copy(update={"location_scope": toulouse_scope})

    def no_offers(config: SearchConfig, store: JobStore, *, progress: cli.ProgressReporter) -> dict[str, int]:
        return {}

    monkeypatch.setattr(cli, "route_location_overrides", resolve)
    monkeypatch.setattr(cli, "scrape_jobspy_sources", no_offers)
    args = [
        "--db",
        str(tmp_path / "jobs.duckdb"),
        "--export-dir",
        str(tmp_path),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    runner = CliRunner()
    live = runner.invoke(cli.app, [*args, "--skip-custom"])
    assert live.exit_code == 0, live.output

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Offline commands must reuse the saved coordinates")

    monkeypatch.setattr(cli, "route_location_overrides", forbidden)
    status = runner.invoke(cli.app, [*args, "--status"])
    assert status.exit_code == 0, status.output
    assert "25 km around Toulouse" in status.stdout
    assert "saved geographic scope" in status.stdout

    offline = runner.invoke(cli.app, [*args, "--export-only", "--show-config"])
    assert offline.exit_code == 0, offline.output
    report = json.loads(offline.stdout)
    assert report["values"]["location_scope"] == toulouse_scope.model_dump(mode="json")
    assert report["sources"]["location_scope.centers"] == "saved geographic scope"

    monkeypatch.setenv("SCRAPPER_DISTANCE_KM", "15")
    changed = runner.invoke(cli.app, [*args, "--export-only", "--show-config"])
    assert changed.exit_code == 0, changed.output
    assert json.loads(changed.stdout)["values"]["location_scope"]["centers"] == []


def test_disabling_aggregators_controls_dispatch_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def welcome_to_the_jungle(config: SearchConfig, store: JobStore) -> int:
        return 3

    async def unexpected(*args: object) -> Never:
        raise AssertionError("The disabled aggregator scraper ran")

    monkeypatch.setattr(cli, "scrape_welcome_to_the_jungle", welcome_to_the_jungle)
    monkeypatch.setattr(cli, "scrape_aggregators", unexpected)
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__AGGREGATORS", "false")
    config = SearchConfig(paths={"database_path": tmp_path / "jobs.duckdb"})
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


def test_local_export_survives_source_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    async def sources(config: SearchConfig, store: JobStore) -> tuple[dict[str, int], dict[str, str]]:
        store.start_search("blocked_board", "example coordinator", "Toulouse")
        store.finish_search("blocked_board", "example coordinator", "Toulouse", 0, error=SourceBlockedError.message)
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

    monkeypatch.setattr(cli, "_run_custom_sources", sources)
    monkeypatch.setattr(
        cli,
        "route_location_overrides",
        lambda config, *, write_cache: config.model_copy(update={"location_scope": toulouse_scope}),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--skip-jobspy",
            "--location",
            "Toulouse, France",
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
    assert result.output.count("blocked_board blocked by a verification challenge.") == 1
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
        raise AssertionError("Export-only must skip scraping and observation pruning")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources"):
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
        raise AssertionError("Replay must never scrape")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources"):
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
