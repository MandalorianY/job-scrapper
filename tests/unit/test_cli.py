import json
from importlib import import_module
from pathlib import Path

from typer.testing import CliRunner

from scrapper import cli
from scrapper.config.settings import PACKAGE_CONFIG_DIR
from scrapper.models import JobOffer


def test_show_config_reports_effective_values_and_origins(monkeypatch, tmp_path: Path) -> None:
    local_config = tmp_path / "config.local.yaml"
    local_config.write_text(
        'queries: [Linux]\nlocations: ["Lille, France"]\ndistance_km: 10\n'
        'enabled_sources:\n  welcome_to_the_jungle: false\n',
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

    def forbidden(*args, **kwargs):
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
    assert values["paths"]["database_path"] == str(tmp_path / "test.duckdb")
    assert sources["app_env"] == str(dotenv)
    assert sources["queries"] == sources["paths.database_path"] == "command line"
    assert sources["locations"] == sources["enabled_sources.welcome_to_the_jungle"] == str(local_config)
    assert sources["enabled_sources.hellowork"] == str(PACKAGE_CONFIG_DIR / "config.yaml")
    assert sources["custom_max_pages"] == str(PACKAGE_CONFIG_DIR / "config.prod.yaml")
    assert sources["results_wanted_per_query"] == "environment"
    assert sources["jobspy_hours_old_override"] == "default"
    assert sources["welcome_to_the_jungle.algolia_api_key"] == str(dotenv)


def test_local_export_survives_source_failure_and_never_syncs(monkeypatch, tmp_path: Path) -> None:
    async def sources(config, store):
        store.upsert_offer(JobOffer(source="example", source_url="https://example.com/jobs/1",
                                    title="Example coordinator", company="Example", location="Toulouse"))
        return {"example": 1}, {"unavailable_board": "Temporary source outage"}

    def forbidden_sync():
        raise AssertionError("Local validation must not fetch or publish the remote tracker")

    monkeypatch.setattr(cli, "_run_custom_sources", sources)
    monkeypatch.setattr(cli, "_tracker_sync_hooks", forbidden_sync)
    result = CliRunner().invoke(cli.app, [
        "--skip-jobspy", "--no-tracker-sync", "--db", str(tmp_path / "jobs.duckdb"),
        "--export-dir", str(tmp_path / "exports"), "--log-file", str(tmp_path / "run.jsonl"),
    ])
    assert result.exit_code == 0, result.output
    assert "unavailable_board failed" in result.output
    assert (tmp_path / "exports/jobs.xlsx").exists()
