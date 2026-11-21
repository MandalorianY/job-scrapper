from pathlib import Path

import pandas as pd
import pytest
import yaml
from openpyxl import load_workbook
from pydantic import ValidationError
from typer.testing import CliRunner

from scrapper.cli import app
from scrapper.config.settings import ExportConfig, ExportFormat
from scrapper.export import export_job_formats
from scrapper.models import JobOffer
from scrapper.storage import JobStore


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
            "--profile",
            str(profile),
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
