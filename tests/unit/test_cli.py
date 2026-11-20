from pathlib import Path

from typer.testing import CliRunner

from scrapper import cli
from scrapper.models import JobOffer


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
