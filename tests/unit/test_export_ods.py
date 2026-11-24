from pathlib import Path
from zipfile import ZipFile

import pandas as pd
import pytest
from openpyxl import load_workbook

from jobsfr.export import export_job_formats
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


@pytest.mark.parametrize("with_status", (True, False))
def test_ods_preserves_saved_tracker_state(tmp_path: Path, with_status: bool) -> None:
    pytest.importorskip("odf")
    columns = ("Postulé", "Rejeté", "Lien", "Intitulé")
    if with_status:
        columns += ("Statut",)

    store = JobStore(tmp_path / "jobs.duckdb")
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
        export_dir = tmp_path / "exports"
        (xlsx, _, _), _ = export_job_formats(store, export_dir, formats=("xlsx", "csv"), excel_columns=columns)
        workbook = load_workbook(xlsx)
        workbook["Offres"]["B2"] = True
        workbook.save(xlsx)
        workbook.close()

        (ods, _, _), total = export_job_formats(store, export_dir, excel_columns=columns, formats=("ods", "csv"))
        assert total == 1
        assert xlsx.exists()
        edited = pd.read_excel(ods, sheet_name="Offres", engine="odf")
        assert bool(edited.at[0, "Rejeté"])
        edited["Rejeté"] = False
        if with_status:
            edited["Statut"] = "Postulé"
        else:
            edited["Postulé"] = True
        with pd.ExcelWriter(ods, engine="odf") as writer:
            edited.to_excel(writer, sheet_name="Offres", index=False)
        localized = export_dir / "localized.ods"
        with ZipFile(ods) as original, ZipFile(localized, "w") as archive:
            for entry in original.infolist():
                data = original.read(entry)
                archive.writestr(entry, data.replace(b">TRUE<", b">VRAI<") if entry.filename == "content.xml" else data)
        localized.replace(ods)

        (ods, csv, raw_csv), total = export_job_formats(
            store, export_dir, excel_columns=columns, formats=("ods", "csv")
        )
        assert total == 1
        assert csv.is_file()
        assert raw_csv.is_file()
        exported = pd.read_excel(ods, sheet_name="Offres", engine="odf")
        assert bool(exported.at[0, "Postulé"])
        assert not bool(exported.at[0, "Rejeté"])
        assert exported.at[0, "Lien"] == "https://example.com/jobs/1"
        with ZipFile(ods) as archive:
            content = archive.read("content.xml")
        assert b'form:linked-cell="Offres.B2"' in content
        assert b'xlink:href="https://example.com/jobs/1"' in content
        if with_status:
            assert exported.at[0, "Statut"] == "Postulé"
            assert b"table:content-validation " in content
            assert b'form:linked-cell="Offres.A2"' not in content
        else:
            assert b'form:linked-cell="Offres.A2"' in content
    finally:
        store.close()
