import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from typer.testing import CliRunner

from jobsfr import cli
from jobsfr.cli import profile_setup as cli_setup
from jobsfr.cli import runner as cli_run
from jobsfr.config import SearchConfig
from jobsfr.export.jobs import read_tracker
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["serach"], "No such command"),
        (["search", "--results-wanted", "0"], "--results-wanted"),
        (["search", "--custom-pages", "-1"], "--custom-pages"),
        (["replay"], "--archive"),
        (["schedule", "remove"], "No such command"),
        (["sync", "--status", "--prefer", "local"], "--prefer applies to a sync run"),
    ],
)
def test_invalid_cli_usage_explains_the_error(args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 2, result.output
    assert message in result.stderr


@pytest.mark.property
@given(st.one_of(st.integers(), st.booleans(), st.text(max_size=20), st.lists(st.text(max_size=10), max_size=4)))
def test_status_rejects_nonmapping_yaml(payload: int | str | list[str]) -> None:
    with TemporaryDirectory() as directory:
        profile = Path(directory) / "profil été.yaml"
        profile.write_text(yaml.safe_dump(payload), encoding="utf-8")
        result = CliRunner().invoke(cli.app, ["status", "--json", "--profile", str(profile)])
        assert result.exit_code == 2, result.output
        assert "Expected a YAML mapping" in result.stderr
        assert not result.stdout


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        (b"\x80", "must be UTF-8"),
        (b"locations: [unclosed", "Invalid YAML configuration"),
        (b"locations: [Lille]\nlocations: [Toulouse]\n", "Duplicate YAML key"),
        (b"paths: {export_dir: first, export_dir: second}", "Duplicate YAML key"),
        (b"location_scope: {raduis_km: 3}", "location_scope.raduis_km"),
        (b"enabled_sources: {hellowrok: false}", "Unknown enabled_sources"),
        (b"custom_max_pages: -1", "custom_max_pages"),
        (b"http_per_host_concurrency: 0", "http_per_host_concurrency"),
        (b"http_request_delay_secs: .inf", "http_request_delay_secs"),
        (b"export: {excel_columns: []}", "At least one Excel column"),
        (b"export: {excel_columns: [Lien, Lien]}", "List each Excel column once"),
        (b"export: {excel_columns: [Intitule]}", "Unknown Excel column"),
    ],
)
def test_search_rejects_bad_profiles_before_writing_artifacts(tmp_path: Path, contents: bytes, message: str) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_bytes(contents)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "--profile",
            str(profile),
            "--query",
            "analyste",
            "--location",
            "Lille",
            "--db",
            str(tmp_path / "jobs.duckdb"),
            "--export-dir",
            str(tmp_path / "exports"),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert message in " ".join(result.stderr.split())
    assert list(tmp_path.iterdir()) == [profile]
    assert profile.read_bytes() == contents


def test_status_reports_malformed_environment_without_echoing_its_value() -> None:
    invalid = "invalid-json-private-value"
    result = CliRunner().invoke(cli.app, ["status", "--json"], env={"JOBSFR_QUERIES": invalid})
    assert result.exit_code == 2, result.output
    assert "Invalid configuration" in result.stderr
    assert "queries" in result.stderr
    assert invalid not in result.output
    assert not result.stdout


@pytest.mark.parametrize("flag", ["JOBSFR_USE_WORKING_DIRECTORY", "JOBSFR_DISABLE_LOCAL_CONFIG"])
def test_status_rejects_on_for_bootstrap_flags(flag: str) -> None:
    result = CliRunner().invoke(cli.app, ["status", "--json"], env={flag: "on"})
    assert result.exit_code == 2, result.output
    assert "Use 1 or true to enable" in " ".join(result.stderr.split())


def test_status_preserves_yaml_merge_overrides(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "location_scope:\n  <<: {radius_km: 10, include_unrecognized: false}\n  radius_km: 3\n", encoding="utf-8"
    )
    result = CliRunner().invoke(cli.app, ["status", "--json", "--profile", str(profile)])
    assert result.exit_code == 0, result.output
    scope = json.loads(result.stdout)["values"]["location_scope"]
    assert scope["radius_km"] == 3
    assert scope["include_unrecognized"] is False


@pytest.mark.parametrize("filename", ["profil été.yaml", ".env"])
def test_replay_cannot_write_logs_over_its_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    monkeypatch.chdir(tmp_path)
    profile = tmp_path / "profil été.yaml"
    profile.write_text("location_scope: {enabled: false}\n", encoding="utf-8")
    dotenv = tmp_path / ".env"
    dotenv.write_text("JOBSFR_OVERLAP_HOURS=24\n", encoding="utf-8")
    archive = tmp_path / "archive.duckdb"
    archive.touch()

    original = {path: path.read_bytes() for path in (profile, dotenv, archive)}
    result = CliRunner().invoke(
        cli.app,
        [
            "replay",
            "--archive",
            str(archive),
            "--profile",
            str(profile),
            "--db",
            str(tmp_path / "jobs.duckdb"),
            "--export-dir",
            str(tmp_path / "exports"),
            "--log-file",
            str(tmp_path / filename),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Log points to a configuration file" in " ".join(result.stderr.split())
    assert {path: path.read_bytes() for path in tmp_path.iterdir()} == original


def test_replay_rejects_an_export_folder_blocked_by_a_file(tmp_path: Path) -> None:
    archive = tmp_path / "archive.duckdb"
    archive.touch()
    existing = tmp_path / "tracker"
    existing.write_bytes(b"existing file")
    result = CliRunner().invoke(
        cli.app,
        [
            "replay",
            "--archive",
            str(archive),
            "--db",
            str(tmp_path / "new.duckdb"),
            "--export-dir",
            str(existing / "exports"),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 2, result.output
    assert "Could not open the export folder" in " ".join(result.stderr.split())
    assert set(tmp_path.iterdir()) == {archive, existing}
    assert existing.read_bytes() == b"existing file"


def test_setup_cancelled_midway_leaves_no_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profile = tmp_path / "config" / "config.yaml"
    monkeypatch.setattr(cli_setup, "default_local_config", lambda: profile)
    result = CliRunner().invoke(cli.app, ["setup"], input="analyste\n")
    assert result.exit_code == 1, result.output
    assert "Aborted" in result.stderr
    assert not profile.parent.exists()


@pytest.mark.parametrize("failed_source", ["jobspy", "aggregators"])
def test_parallel_search_exports_when_a_source_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_source: str
) -> None:
    profile = tmp_path / "profile.yaml"
    profile.write_text("location_scope: {enabled: false}\n", encoding="utf-8")
    export_dir = tmp_path / "exports"

    def jobspy(config: SearchConfig, store: JobStore, *, progress: cli_run.ProgressReporter | None) -> dict[str, int]:
        if failed_source == "jobspy":
            raise TimeoutError
        assert progress is not None
        progress("JobSpy done [1/1]: indeed")
        return {"indeed": 0}

    async def aggregators(config: SearchConfig, store: JobStore) -> dict[str, int]:
        if failed_source == "aggregators":
            raise TimeoutError
        return {"jobijoba": 0}

    async def wttj(config: SearchConfig, store: JobStore) -> int:
        store.upsert_offer(
            JobOffer(
                source="welcome_to_the_jungle",
                source_url="https://example.test/jobs/analyste",
                title="Analyste",
                company="Example",
                employment_type="CDI",
                location="Lille",
            )
        )
        return 1

    monkeypatch.setattr(cli_run, "route_location_overrides", lambda config, *, write_cache: config)
    monkeypatch.setattr(cli_run, "scrape_jobspy_sources", jobspy)
    monkeypatch.setattr(cli_run, "scrape_aggregators", aggregators)
    monkeypatch.setattr(cli_run, "scrape_welcome_to_the_jungle", wttj)
    result = CliRunner().invoke(
        cli.app,
        [
            "search",
            "--profile",
            str(profile),
            "--query",
            "analyste",
            "--location",
            "Lille",
            "--db",
            str(tmp_path / "jobs.duckdb"),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert f"{failed_source} failed: TimeoutError" in result.stdout
    assert read_tracker(export_dir / "jobs.xlsx")["Intitulé"].to_list() == ["Analyste"]
