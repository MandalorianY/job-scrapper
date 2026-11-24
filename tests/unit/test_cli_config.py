import json
from importlib import import_module
from pathlib import Path
from typing import Never

import httpx
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from jobsfr import CLI_NAME, __version__, cli
from jobsfr.cli import profile_setup as cli_setup
from jobsfr.cli import runner as cli_run
from jobsfr.config import SearchConfig
from jobsfr.config.settings import EXAMPLE_CONFIG_FILE, PACKAGE_CONFIG_DIR
from jobsfr.geography import regions as search_regions
from jobsfr.geography.scope import location_is_in_scope
from jobsfr.offers.filters import has_excluded_contract_signal
from jobsfr.scrapers.aggregators import site_search_templates


def test_search_configuration_rejects_handwritten_coordinates() -> None:
    with pytest.raises(ValidationError, match="The scraper resolves coordinates automatically"):
        SearchConfig(location_scope={"city_coordinates": {"Paris": [48.8589, 2.347]}})


@pytest.mark.parametrize("field", ["title", "description", "contract"])
def test_search_configuration_rejects_invalid_patterns(field: str) -> None:
    with pytest.raises(ValidationError, match=rf"filters\.{field}\.exclude"):
        SearchConfig(filters={field: {"exclude": [{"regex": "["}]}})


def test_search_overrides_do_not_inherit_placeholder_title_filters() -> None:
    config = SearchConfig(queries=("analyste",), locations=("Lille",), _require_search_setup=True)

    assert config.filters.title.allows("Analyste")


def test_builtin_contract_policy_uses_bundled_exclusions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JOBSFR_DISABLE_LOCAL_CONFIG", "1")
    exclusions = SearchConfig().filters.contract.exclude
    for term in ("internship", "alternance", "apprentissage", "contrat de professionnalisation"):
        assert term in exclusions
        assert has_excluded_contract_signal(f"Example role ({term})", "CDI", None)
        assert has_excluded_contract_signal("Example role", term, None)

    profile = SearchConfig(filters={"contract": {"exclude": ["CDD"]}})
    assert not profile.filters.contract.allows("CDD")
    assert not has_excluded_contract_signal("Example role", "CDD", None)
    assert has_excluded_contract_signal("Example role", "internship", None)


def test_company_filter_excludes_named_employers_and_rejects_invalid_rules() -> None:
    config = SearchConfig(filters={"company": {"exclude": {"names": ["Example Agency"]}}})
    assert not config.filters.company.allows("Example Agency SAS")
    with pytest.raises(ValidationError, match=r"filters\.company\.exclude"):
        SearchConfig(filters={"company": {"exclude": ["Example Agency"]}})
    with pytest.raises(ValidationError, match="Unknown company types in the community catalog: esnn"):
        SearchConfig(filters={"company": {"exclude": {"types": ["esnn"]}}})


def test_search_configuration_rejects_unknown_settings() -> None:
    with pytest.raises(ValidationError, match="unknown_setting"):
        SearchConfig(unknown_setting=True)


def test_company_filter_reads_new_types_and_quoted_names_from_csv(tmp_path: Path) -> None:
    catalog = tmp_path / "company_types.csv"
    catalog.write_text(
        'company,type,sources\n"École, Exemple",training_provider,https://example.test\n',
        encoding="utf-8-sig",
    )
    config = SearchConfig(
        company_catalog_path=catalog,
        filters={"company": {"exclude": {"types": ["training_provider"]}}},
    )
    assert not config.filters.company.allows("École, Exemple SAS")
    assert config.filters.company.allows("École, Exemple Direct")


@pytest.mark.parametrize(
    ("header", "records", "check"),
    [
        ("company,type,sources", "", "has_entries"),
        ("company,type,sources", "...,esn,https://example.test\n", "usable_company_name"),
        ("company,type,sources", "Example,Wrong Type,https://example.test\n", "Column 'type'"),
        ("company,type,sources", "Example,esn,example.test\n", "Column 'sources'"),
        ("company,type,sources,extra", "Example,esn,https://example.test,extra\n", "column 'extra'"),
        (
            "company,type,sources",
            "Example,esn,https://example.test\nExample SAS,esn,https://example.test\n",
            "unique_company_type",
        ),
    ],
)
def test_company_filter_reports_catalog_schema_errors(tmp_path: Path, header: str, records: str, check: str) -> None:
    catalog = tmp_path / "company_types.csv"
    catalog.write_text(f"{header}\n{records}", encoding="utf-8")
    with pytest.raises(ValidationError, match=rf"(?s)company_types\.csv: .*{check}"):
        SearchConfig(company_catalog_path=catalog, filters={"company": {"exclude": {"types": ["esn"]}}})


def test_setup_creates_a_search_profile_without_replacing_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "config.local.yaml"
    monkeypatch.setattr(cli_setup, "default_local_config", lambda: profile)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["setup"], input="Chargé d'études\nToulouse\n")

    assert result.exit_code == 0, result.output
    assert str(EXAMPLE_CONFIG_FILE) in result.output
    assert "jobsfr schedule enable" in result.output
    assert "jobsfr search" in result.output
    config = SearchConfig(_profile=profile, _require_search_setup=True)
    assert config.queries == ("Chargé d'études",)
    assert config.locations == ("Toulouse",)
    assert config.filters.title.allows("Chargé d'études H/F")
    saved = profile.read_bytes()

    result = runner.invoke(cli.app, ["setup"])
    assert result.exit_code == 2
    assert "already exists" in result.output
    assert profile.read_bytes() == saved

    result = runner.invoke(cli.app, ["setup", "--example"])
    assert result.exit_code == 0, result.output
    assert result.stdout == EXAMPLE_CONFIG_FILE.read_text(encoding="utf-8")
    assert profile.read_bytes() == saved
    example = SearchConfig(_profile=EXAMPLE_CONFIG_FILE, _require_search_setup=True)
    assert example.filters.allows({"title": "Data Analyst", "company": "Some Employer", "employment_type": "CDI"}.get)


def test_version_and_explicit_profile_work_with_invalid_local_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text("paths: [invalid\n", encoding="utf-8")
    profile = tmp_path / "selected.yaml"
    profile.write_text("queries: [selected]\n", encoding="utf-8")
    monkeypatch.setattr(import_module("jobsfr.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG", raising=False)
    runner = CliRunner()

    version = runner.invoke(cli.app, ["--version"])
    assert version.exit_code == 0
    assert version.stdout.strip() == f"{CLI_NAME} {__version__}"
    bad_config = runner.invoke(cli.app, ["status"])
    assert bad_config.exit_code == 2
    assert "Invalid YAML configuration" in bad_config.output
    selected = runner.invoke(cli.app, ["status", "--json", "--profile", str(profile)])
    assert selected.exit_code == 0, selected.output
    assert json.loads(selected.stdout)["values"]["queries"] == ["selected"]


def test_status_stays_offline_when_geography_is_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Configuration preview must not open an HTTP client")

    monkeypatch.setattr(search_regions.httpx, "Client", forbidden)
    profile = tmp_path / "profile.yaml"
    profile.write_text('locations: ["Paris, France"]\n', encoding="utf-8")
    database = tmp_path / "missing.duckdb"
    cache = tmp_path / ".geography" / "town-paris.json"
    cache.parent.mkdir()
    cache.write_text("invalid json", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["status", "--json", "--db", str(database), "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["values"]["locations"] == ["Paris, France"]
    assert not database.exists()
    assert cache.read_text(encoding="utf-8") == "invalid json"


def test_installed_config_and_cli_paths_use_the_home_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    workdir = tmp_path / "tracker"
    workdir.mkdir()
    settings_module = import_module("jobsfr.config.settings")
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(settings_module, "PROJECT_ROOT", None)
    monkeypatch.setattr(settings_module, "LOCAL_CONFIG_FILE", tmp_path / "package" / "config.local.yaml")
    config_dir = home / "app config"
    monkeypatch.setattr(settings_module, "user_config_dir", lambda: config_dir)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG", raising=False)
    monkeypatch.delenv("JOBSFR_USE_WORKING_DIRECTORY", raising=False)
    default_queries = SearchConfig().queries
    (workdir / "config.local.yaml").write_text("queries: [local]\n", encoding="utf-8")
    (workdir / "config.yaml").write_text("queries: [working directory]\n", encoding="utf-8")
    profile = home / "profile.yaml"
    profile.write_text(
        'queries: [profile]\npaths:\n  database_path: "~/jobs.duckdb"\n  export_dir: "~/exports"\n'
        '  log_file: "~/jobs.jsonl"\n',
        encoding="utf-8",
    )

    assert SearchConfig().queries == default_queries
    runner = CliRunner()
    result = runner.invoke(cli.app, ["setup"], input="installed\nToulouse\n")
    assert result.exit_code == 0, result.output
    assert (config_dir / "config.yaml").is_file()
    (config_dir / ".env").write_text(
        "JOBSFR_APP_ENV=prod\nJOBSFR_USE_WORKING_DIRECTORY=true\nJOBSFR_DISABLE_LOCAL_CONFIG=true\n"
        "OTHER_TOOL_SETTING=shared\n",
        encoding="utf-8",
    )
    config = SearchConfig(_require_search_setup=True)
    assert config.queries == ("installed",)
    assert config.custom_max_pages == 3

    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Search profile config.yaml (overrides applied)" in " ".join(result.stdout.split())
    result = runner.invoke(cli.app, ["status", "--json", "--profile", "~/profile.yaml"])
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["values"]["queries"] == ["profile"]
    assert report["values"]["paths"] == {
        "database_path": str(home / "jobs.duckdb"),
        "export_dir": str(home / "exports"),
        "log_file": str(home / "jobs.jsonl"),
    }
    assert report["sources"]["paths.database_path"] == str(profile)
    assert report["sources"]["paths.export_dir"] == str(profile)
    assert report["sources"]["app_env"] == str(config_dir / ".env")

    monkeypatch.setenv("JOBSFR_USE_WORKING_DIRECTORY", "true")
    config = SearchConfig()
    assert config.queries == ("local",)
    assert config.database_path.resolve() == (workdir / "data/job_search.duckdb").resolve()
    assert config.export_dir.resolve() == (workdir / "exports").resolve()
    assert config.log_file.resolve() == (workdir / "data/jobsfr.jsonl").resolve()

    monkeypatch.setenv("JOBSFR_DISABLE_LOCAL_CONFIG", "1")
    assert SearchConfig().queries == default_queries
    assert SearchConfig(_profile=profile).queries == ("profile",)


def test_status_json_reports_effective_values_and_origins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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
        "JOBSFR_APP_ENV=prod\nJOBSFR_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY=private-key\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(import_module("jobsfr.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG")
    monkeypatch.delenv("JOBSFR_APP_ENV", raising=False)
    monkeypatch.setenv("JOBSFR_EXTRA_HEADERS", '{"Authorization": "private-header"}')
    monkeypatch.setenv("JOBSFR_RESULTS_WANTED_PER_QUERY", "30")
    monkeypatch.setenv("JOBSFR_AGGREGATORS__MAX_DETAIL_PAGES", "25")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Printing configuration must have no run side effects")

    for name in ("configure_logging", "_job_store"):
        monkeypatch.setattr(cli_run, name, forbidden)

    result = CliRunner().invoke(cli.app, ["status", "--json", "--db", "test.duckdb"])
    assert result.exit_code == 0, result.output
    assert "private-key" not in result.output
    assert "private-header" not in result.output
    report = json.loads(result.stdout)
    values, sources = report["values"], report["sources"]
    assert values["app_env"] == "prod"
    assert values["queries"] == ["Linux"]
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
    assert sources["paths.database_path"] == "command line"
    assert sources["queries"] == str(local_config)
    assert sources["locations"] == sources["enabled_sources.welcome_to_the_jungle"] == str(local_config)
    assert sources["enabled_sources.apec"] == str(PACKAGE_CONFIG_DIR / "config.yaml")
    assert sources["custom_max_pages"] == str(PACKAGE_CONFIG_DIR / "config.prod.yaml")
    assert sources["results_wanted_per_query"] == "environment"
    assert sources["aggregators.location_terms"] == str(local_config)
    assert sources["aggregators.direct_specs"] == sources["aggregators.site_search_sources"] == str(local_config)
    assert sources["aggregators.max_detail_pages"] == "environment"
    assert sources["jobspy_hours_old_override"] == "default"
    assert sources["welcome_to_the_jungle.algolia_api_key"] == str(dotenv)


@pytest.mark.parametrize("explicit_profile", [False, True])
def test_yaml_app_env_selects_overlay_with_environment_precedence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, explicit_profile: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("JOBSFR_APP_ENV", raising=False)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG")
    profile = tmp_path / "profile.yaml"
    profile.write_text("app_env: prod\naggregators: {max_detail_pages: 17}\n", encoding="utf-8")
    args = ["status", "--json"]
    if explicit_profile:
        args += ["--profile", str(profile)]
    else:
        monkeypatch.setattr(import_module("jobsfr.config.settings"), "LOCAL_CONFIG_FILE", profile)

    runner = CliRunner()
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["values"]["app_env"] == "prod"
    assert report["values"]["custom_max_pages"] == 3
    assert report["values"]["aggregators"]["max_detail_pages"] == 17
    assert report["sources"]["app_env"] == str(profile)
    assert report["sources"]["custom_max_pages"] == str(PACKAGE_CONFIG_DIR / "config.prod.yaml")

    (tmp_path / ".env").write_text("JOBSFR_APP_ENV=dev\n", encoding="utf-8")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["values"]["app_env"] == "dev"
    assert report["values"]["custom_max_pages"] == 2

    monkeypatch.setenv("JOBSFR_APP_ENV", "prod")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    report = json.loads(result.stdout)
    assert report["values"]["app_env"] == "prod"
    assert report["values"]["custom_max_pages"] == 3
    assert report["sources"]["app_env"] == "environment"
    assert SearchConfig(_profile=profile, app_env="dev").custom_max_pages == 2


def test_status_inspects_search_configuration_without_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        'queries: [analytics]\nlocations: ["Lille, France"]\n'
        "filters:\n"
        "  title: {include: [data analyst], exclude: [{regex: senior}]}\n"
        "  contract: {exclude: [temporary]}\n"
        "  by_source: {linkedin: {company: {exclude: {names: [Example Company]}}}}\n"
        "aggregators:\n  max_detail_pages: 42\n  direct_locations: [32R]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(import_module("jobsfr.config.settings"), "LOCAL_CONFIG_FILE", local_config)
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG", raising=False)
    monkeypatch.setenv("JOBSFR_DISTANCE_KM", "10")
    monkeypatch.setenv("JOBSFR_ENABLED_SOURCES__GOOGLE", "false")
    monkeypatch.setenv("JOBSFR_ENABLED_SOURCES__AGGREGATORS", "false")
    monkeypatch.setenv("JOBSFR_PATHS__DATABASE_PATH", str(tmp_path / "jobs.duckdb"))
    monkeypatch.setenv("JOBSFR_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY", "private-key")

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Status must be offline and have no run side effects")

    for name in ("configure_logging", "_job_store", "route_location_overrides"):
        monkeypatch.setattr(cli_run, name, forbidden)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    for value in ("Lille, France", "10 km", "analytics", "data analyst", "temporary", "senior", "Example Company"):
        assert value in result.stdout
    assert "Search profile config.local.yaml (overrides applied)" in " ".join(result.stdout.split())
    assert "config.local.yaml: aggregators, filters, locations, queries" in result.stdout
    assert "environment: distance_km, enabled_sources" in result.stdout
    assert "42 aggregator detail pages" in " ".join(result.stdout.split())
    assert "private-key" not in result.stdout
    rows = [
        tuple(cell.strip() for cell in line.strip("│").split("│"))
        for line in result.stdout.splitlines()
        if line.startswith("│")
    ]
    assert ("indeed", "JobSpy", "Enabled") in rows
    assert ("google", "JobSpy", "Disabled") in rows
    assert ("hellowork", "JobSpy", "Enabled") in rows
    assert ("welcome_to_the_jungle", "Custom", "Enabled") in rows
    assert ("emploi_territorial", "Aggregator", "Disabled") in rows
    assert ("cadremploi", "Aggregator", "Disabled") in rows
    assert "32R" in result.stdout

    profile = tmp_path / "config.yaml"
    profile.write_text("queries: [graphiste]\n", encoding="utf-8")
    result = runner.invoke(cli.app, ["status", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    assert "Search profile config.yaml (overrides applied)" in " ".join(result.stdout.split())
    assert "config.yaml: queries" in result.stdout
    assert "config.local.yaml:" not in result.stdout

    monkeypatch.setenv("JOBSFR_DISABLE_LOCAL_CONFIG", "1")
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0, result.output
    assert "Search profile None" in " ".join(result.stdout.split())
    assert "config.local.yaml for local settings." in " ".join(result.stdout.split())
    assert "Use --profile FILE for a specific YAML profile." in " ".join(result.stdout.split())


@pytest.mark.parametrize(
    ("yaml_enabled", "indicator"),
    [(None, "✕ Disabled"), (True, "✓ Enabled"), (False, "✕ Disabled")],
)
def test_status_shows_tracker_sync_from_yaml(tmp_path: Path, yaml_enabled: bool | None, indicator: str) -> None:
    database = tmp_path / "jobs.duckdb"
    args = ["status", "--db", str(database)]
    if yaml_enabled is not None:
        profile = tmp_path / "profile.yaml"
        profile.write_text(
            f'sync:\n  enabled: {str(yaml_enabled).lower()}\n  target: "private-remote:jobs.xlsx"\n', encoding="utf-8"
        )
        args.extend(["--profile", str(profile)])
    result = CliRunner().invoke(cli.app, args)

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

    profile = tmp_path / "designer.yaml"
    profile.write_text(
        'locations: ["Paris, France"]\nqueries: [graphiste]\nfilters: {title: {include: [graphiste]}}\ndistance_km: 15\n',
        encoding="utf-8",
    )
    local = tmp_path / "config.local.yaml"
    local.write_text(
        'locations: ["Toulouse, France"]\nqueries: [marketing]\njobspy:\n  google_query_places: [Toulouse]\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("JOBSFR_DISABLE_LOCAL_CONFIG")
    monkeypatch.setattr(import_module("jobsfr.config.settings"), "LOCAL_CONFIG_FILE", local)

    runner = CliRunner()
    status_args = ["status", "--db", str(original.database_path), "--profile", str(profile)]
    text_status = runner.invoke(cli.app, status_args)
    assert text_status.exit_code == 0, text_status.output
    assert "15 km around Paris" in text_status.stdout
    assert "Derived from search locations" in text_status.stdout
    json_status = runner.invoke(cli.app, [*status_args, "--json"])
    assert json_status.exit_code == 0, json_status.output
    report = json.loads(json_status.stdout)
    assert report["values"]["jobspy"]["google_query_places"] == []
    assert report["values"]["aggregators"]["direct_locations"] == []
    assert report["values"]["queries"] == ["graphiste"]
    assert report["values"]["location_scope"]["radius_km"] == 15
    assert report["values"]["location_scope"]["centers"][0]["name"] == "Paris"
    assert report["sources"]["queries"] == str(profile)
    assert requests == []
