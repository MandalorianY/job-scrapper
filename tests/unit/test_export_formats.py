from pathlib import Path

import pandas as pd
import pytest
import yaml
from openpyxl import load_workbook
from pydantic import ValidationError
from typer.testing import CliRunner

from jobsfr.cli import app
from jobsfr.config.settings import ExportConfig, ExportFormat
from jobsfr.export import export_job_formats
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


@pytest.fixture
def database(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.duckdb"
    store = JobStore(path)
    try:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse",
                description="Full-time role coordinating projects.",
            )
        )
    finally:
        store.close()
    return path


@pytest.mark.parametrize(
    ("formats", "expected"),
    (
        (("xlsx",), {"jobs.xlsx"}),
        (("csv",), {"jobs.csv", "jobs_raw.csv"}),
        (("ods",), {"jobs.ods"}),
        (("xlsx", "ods"), {"jobs.xlsx", "jobs.ods"}),
        (("ods", "xlsx", "csv"), {"jobs.ods", "jobs.xlsx", "jobs.csv", "jobs_raw.csv"}),
    ),
)
def test_cli_exports_selected_formats(
    tmp_path: Path, database: Path, formats: tuple[ExportFormat, ...], expected: set[str]
) -> None:
    if "ods" in formats:
        pytest.importorskip("odf")
    profile = tmp_path / "profile.yaml"
    profile.write_text(yaml.safe_dump({"export": {"formats": list(formats)}}), encoding="utf-8")
    export_dir = tmp_path / "exports"
    result = CliRunner().invoke(
        app,
        [
            "export",
            "--profile",
            str(profile),
            "--db",
            str(database),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert {path.name for path in export_dir.iterdir()} == expected


def test_selected_formats_share_the_first_trackers_status(tmp_path: Path, database: Path) -> None:
    pytest.importorskip("odf")
    store = JobStore(database)
    try:
        columns = ("Statut", "Lien", "Intitulé")
        formats = ("ods", "xlsx", "csv")
        (ods, xlsx, csv, _), _ = export_job_formats(store, tmp_path, columns, formats=formats)

        edited = pd.read_excel(ods, sheet_name="Offres", engine="odf")
        edited["Statut"] = "Postulé"
        with pd.ExcelWriter(ods, engine="odf") as writer:
            edited.to_excel(writer, sheet_name="Offres", index=False)
        workbook = load_workbook(xlsx)
        workbook["Offres"]["A2"] = "Refusé"
        workbook.save(xlsx)
        workbook.close()

        _, total = export_job_formats(store, tmp_path, columns, formats=formats)
        assert total == 1
        assert pd.read_csv(csv).at[0, "Statut"] == "Postulé"
        workbook = load_workbook(xlsx)
        try:
            assert workbook["Offres"]["A2"].value == "Postulé"
        finally:
            workbook.close()
    finally:
        store.close()


@pytest.mark.parametrize("formats", ([], ["pdf"], ["ods", "ods"]))
def test_invalid_export_formats_are_rejected(formats: list[str]) -> None:
    with pytest.raises(ValidationError, match="formats"):
        ExportConfig.model_validate({"formats": formats})


@pytest.mark.parametrize(
    ("name", "contents"),
    [("jobs.xlsx", b"unreadable workbook"), ("jobs.csv", b"date,amount\n2026-01-01,100\n")],
)
def test_export_preserves_an_unreadable_primary_tracker(
    tmp_path: Path, database: Path, name: str, contents: bytes
) -> None:
    profile = tmp_path / "profile.yaml"
    tracker = tmp_path / name
    profile.write_text(f"export: {{formats: [{tracker.suffix[1:]}]}}\n", encoding="utf-8")
    tracker.write_bytes(contents)
    result = CliRunner().invoke(
        app,
        [
            "export",
            "--profile",
            str(profile),
            "--db",
            str(database),
            "--export-dir",
            str(tmp_path),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Could not read existing tracker" in result.stderr
    assert tracker.read_bytes() == contents
    assert not (tmp_path / "jobs_raw.csv").exists()


def test_export_rolls_back_published_files_on_partial_failure(
    tmp_path: Path, database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    export_dir = tmp_path / "exports"
    store = JobStore(database)
    try:
        paths, _ = export_job_formats(store, export_dir, formats=("xlsx", "csv"))
        store.upsert_offer(
            JobOffer(source="example", source_url="https://example.com/jobs/new", title="Analyste", company="Example")
        )
    finally:
        store.close()
    before = {path.name: path.read_bytes() for path in paths}
    original_replace = Path.replace
    attempted: list[str] = []

    def replace(src: Path, target: Path) -> Path:
        if src.parent.name.startswith(".jobs-"):
            attempted.append(target.name)
            if target.name == "jobs.csv":
                raise PermissionError("Existing tracker is open.")
        return original_replace(src, target)

    monkeypatch.setattr(Path, "replace", replace)
    result = CliRunner().invoke(
        app,
        [
            "export",
            "--db",
            str(database),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Existing tracker is open." in result.stderr
    assert attempted == ["jobs.xlsx", "jobs.csv"]
    assert {path.name: path.read_bytes() for path in export_dir.iterdir()} == before
