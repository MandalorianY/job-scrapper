from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from zipfile import ZipFile

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from jobsfr.cli import app
from jobsfr.export import export_job_formats
from jobsfr.export.xlsx import read_xlsx_tracker
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore

_ORIGINAL = JobOffer(
    source="indeed",
    source_id="original",
    source_url="https://indeed.example/job/original",
    direct_url="https://employer.example/jobs/7654",
    title="Chargé de communication",
    company="Example employer",
    location="Toulouse",
    date_posted="2026-10-01",
    description="Vous concevez le calendrier éditorial et les campagnes pour les rendez-vous du service culturel.",
)
_REPOST = replace(_ORIGINAL, source_id="new", source_url="https://indeed.example/job/new", date_posted="2026-10-12")


@pytest.mark.parametrize(
    ("override", "include_repost", "expected"),
    [
        ("", True, ["Republié", "En Attente"]),
        ("export: {keep_reposts: false}\n", True, ["En Attente"]),
        ("export: {excel_columns: [Intitulé]}\n", False, None),
    ],
    ids=["enabled-by-default", "explicitly-disabled", "custom-layout-without-reposts"],
)
def test_cli_repost_default_and_profile_override(
    tmp_path: Path, override: str, include_repost: bool, expected: list[str] | None
) -> None:
    database = tmp_path / "jobs.duckdb"
    store = JobStore(database)
    try:
        store.upsert_offer(_ORIGINAL)
        if include_repost:
            store.upsert_offer(_REPOST)
    finally:
        store.close()
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "location_scope: {enabled: false}\nfilters: {title: {include: [communication]}}\n" + override,
        encoding="utf-8",
    )
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
    assert result.exit_code == 0, result.output
    tracker = read_xlsx_tracker(tmp_path / "jobs.xlsx")
    if expected is None:
        assert list(tracker.columns) == ["Intitulé"] and len(tracker) == 1
    else:
        assert tracker["Statut"].to_list() == expected


def test_reposts_have_independent_status_and_shared_apply_links(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    output = tmp_path / "exports"
    columns = ("Statut", "Lien", "Postuler", "Publié le", "Intitulé")
    try:
        store.upsert_offer(_ORIGINAL)
        (path, _, _), _ = export_job_formats(store, output, columns, formats=("xlsx", "csv"), keep_reposts=True)
        workbook = load_workbook(path)
        workbook["Offres"]["A2"] = "Postulé"
        workbook.save(path)
        workbook.close()

        store.upsert_offer(_REPOST)
        store.upsert_offer(
            replace(
                _REPOST,
                source="linkedin",
                source_url="https://linkedin.example/job/partner",
                date_posted="2026-10-11",
            )
        )
        for _ in range(2):
            _, count = export_job_formats(store, output, columns, formats=("xlsx", "csv"), keep_reposts=True)
            assert count == 2
            rows = read_xlsx_tracker(path)
            assert rows["Statut"].to_list() == ["Republié", "Postulé"]
            assert [value.strftime("%Y-%m-%d") for value in rows["Publié le"]] == ["2026-10-12", "2026-10-01"]
            assert rows.loc[0, "Lien"] != rows.loc[1, "Lien"]
            assert rows.loc[0, "Postuler"] == rows.loc[1, "Postuler"]
        workbook = load_workbook(path)
        sheet = workbook["Offres"]
        assert '"En Attente,Republié,Postulé,Refusé"' == sheet.data_validations.dataValidation[0].formula1
        sheet["A2"] = "Refusé"
        workbook.save(path)
        workbook.close()
        _, count = export_job_formats(store, output, columns, formats=("xlsx", "csv"), keep_reposts=True)
        assert count == 2
        assert read_xlsx_tracker(path)["Statut"].to_list() == ["Refusé", "Postulé"]
        _, count = export_job_formats(store, output, columns, formats=("xlsx", "csv"))
        assert count == 1
        assert read_xlsx_tracker(path)["Statut"].to_list() == ["Refusé"]
        assert len(store.raw_dataframe()) == 3
    finally:
        store.close()


@pytest.mark.parametrize(
    "other",
    [
        replace(_REPOST, date_posted="2026-10-03"),
        replace(_REPOST, date_posted=None),
        replace(_REPOST, source_id=_ORIGINAL.source_id),
        replace(_REPOST, source="linkedin"),
    ],
    ids=["date-drift", "missing-date", "same-listing-id", "different-board-only"],
)
def test_syndication_and_metadata_changes_keep_one_tracker_row(tmp_path: Path, other: JobOffer) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(_ORIGINAL)
        store.upsert_offer(other)
        _, count = export_job_formats(store, tmp_path / "exports", formats=("xlsx",), keep_reposts=True)
        assert count == 1
    finally:
        store.close()


def test_enabling_reposts_relabels_pending_rows_from_a_previous_export(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    output = tmp_path / "exports"
    columns = ("Statut", "Lien", "Publié le")
    try:
        store.upsert_offer(replace(_ORIGINAL, scraped_at="2026-10-01T12:00:00Z"))
        store.upsert_offer(replace(_REPOST, scraped_at="2026-10-12T12:00:00Z"))
        links = {"indeed": False}
        (path,), count = export_job_formats(
            store, output, columns, formats=("xlsx",), direct_links_enabled_by_source=links
        )
        assert count == 1 and _REPOST.source_url in read_xlsx_tracker(path).loc[0, "Lien"]
        _, count = export_job_formats(
            store, output, columns, formats=("xlsx",), direct_links_enabled_by_source=links, keep_reposts=True
        )
        assert count == 2
        assert read_xlsx_tracker(path)["Statut"].to_list() == ["Republié", "En Attente"]
    finally:
        store.close()


def test_ods_keeps_multiple_reposts_and_the_status_dropdown(tmp_path: Path) -> None:
    pytest.importorskip("odf")
    from jobsfr.export.ods import read_ods_tracker

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(_ORIGINAL)
        store.upsert_offer(_REPOST)
        store.upsert_offer(
            replace(_REPOST, source_id="third", source_url="https://indeed.example/job/third", date_posted="2026-10-29")
        )
        (path,), count = export_job_formats(
            store, tmp_path / "exports", ("Intitulé", "Publié le"), formats=("ods",), keep_reposts=True
        )
        assert count == 3
        tracker = read_ods_tracker(path)
        assert tracker["Statut"].to_list() == ["Republié", "Republié", "En Attente"]
        assert len(set(tracker["Lien"])) == 3
        with ZipFile(path) as archive:
            content = archive.read("content.xml").decode("utf-8")
        assert "Republié" in content and "cell-content-is-in-list" in content
    finally:
        store.close()
