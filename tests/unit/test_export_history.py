from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import load_workbook

from scrapper.config.settings import LocationScopeConfig
from scrapper.export import export_job_formats
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore


def _export_refreshed_linkedin_offer(
    tmp_path: Path, header: str, value: str | bool, excel_columns: tuple[str, ...] | None = None
) -> tuple[tuple[Path, ...], int]:
    export_dir = tmp_path / "exports"
    offer = JobOffer(
        source="linkedin",
        source_url="https://linkedin.example/jobs/1",
        title="Example manager",
        company="Example",
        location="Toulouse",
    )
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(offer)
        (excel_path, _, _), _ = export_job_formats(
            store, export_dir, formats=("xlsx", "csv"), excel_columns=(header, "Lien", "Intitulé", "Entreprise")
        )
    finally:
        store.close()

    workbook = load_workbook(excel_path, data_only=False)
    worksheet = workbook["Offres"]
    worksheet["A2"] = value
    workbook.save(excel_path)
    workbook.close()

    refreshed_store = JobStore(tmp_path / "refreshed.duckdb")
    try:
        refreshed_store.upsert_offer(replace(offer, source_url=f"{offer.source_url}?utm_source=refresh"))
        return export_job_formats(refreshed_store, export_dir, formats=("xlsx", "csv"), excel_columns=excel_columns)
    finally:
        refreshed_store.close()


@pytest.mark.parametrize("status", ["Postulé", "Refusé"])
def test_export_preserves_existing_status_for_matching_tracker_rows(tmp_path: Path, status: str) -> None:
    (excel_path, csv_path, _), total = _export_refreshed_linkedin_offer(
        tmp_path, "Statut", status, excel_columns=("Statut", "Lien", "Intitulé", "Entreprise")
    )

    assert total == 1
    assert pd.read_csv(csv_path).loc[0, "Statut"] == status
    worksheet = load_workbook(excel_path, data_only=False)["Offres"]
    assert worksheet["A2"].value == status


@pytest.mark.parametrize(
    ("old_header", "old_value", "expected_status", "applied"),
    [
        ("Statut", "En Attente", "En Attente", False),
        ("Statut", "Postulé", "Postulé", True),
        ("Statut", "Refusé", "Refusé", False),
        ("Postulé", True, "Postulé", True),
    ],
)
def test_export_drives_applied_checkbox_from_status(
    tmp_path: Path, old_header: str, old_value: str | bool, expected_status: str, applied: bool
) -> None:
    (excel_path, csv_path, _), total = _export_refreshed_linkedin_offer(
        tmp_path, old_header, old_value, excel_columns=("Postulé", "Lien", "Statut", "Intitulé")
    )
    assert total == 1
    with excel_path.open("rb") as stream:
        workbook = load_workbook(stream)
        worksheet = workbook["Offres"]
        assert worksheet["A2"].value == '=C2="Postulé"'
        assert worksheet["C2"].value == expected_status
        workbook.close()
    with excel_path.open("rb") as stream:
        workbook = load_workbook(stream, data_only=True)
        assert workbook["Offres"]["A2"].value is applied
        workbook.close()
    assert bool(pd.read_csv(csv_path).loc[0, "Postulé"]) is applied


def test_export_retains_existing_tracker_rows_when_current_export_has_new_rows(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    old_store = JobStore(tmp_path / "old.duckdb")
    try:
        old_store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://linkedin.example/jobs/old",
                title="Old example manager",
                company="Example",
                location="Toulouse",
            )
        )
        export_job_formats(
            old_store, export_dir, formats=("xlsx", "csv"), excel_columns=("Statut", "Lien", "Intitulé", "Entreprise")
        )
    finally:
        old_store.close()

    new_store = JobStore(tmp_path / "new.duckdb")
    try:
        new_store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://linkedin.example/jobs/new",
                title="New example manager",
                company="Example",
                location="Toulouse",
            )
        )

        (excel_path, csv_path, _raw_csv_path), total = export_job_formats(
            new_store,
            export_dir,
            formats=("xlsx", "csv"),
            excel_columns=("Statut", "Lien", "Intitulé", "Entreprise"),
        )

        assert total == 2
        frame = pd.read_csv(csv_path)
        assert frame["Intitulé"].to_list() == ["New example manager", "Old example manager"]
        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet.tables["TableOffres"].ref == "A1:D3"
    finally:
        new_store.close()


@pytest.mark.parametrize(
    ("source_url", "fragment"),
    [
        ("https://linkedin.example/jobs/old", ""),
        ("https://www.hellowork.com/fr-fr/emplois/1.html", "#postuler"),
    ],
)
def test_export_rewrites_retained_ouvrir_links_to_full_url_labels(
    tmp_path: Path,
    source_url: str,
    fragment: str,
) -> None:
    export_dir = tmp_path / "exports"
    old_store = JobStore(tmp_path / "old.duckdb")
    try:
        old_store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url=source_url,
                title="Old example manager",
                company="Example",
                location="Toulouse",
            )
        )
        (excel_path, _csv_path, _raw_csv_path), _total = export_job_formats(
            old_store, export_dir, formats=("xlsx", "csv")
        )
    finally:
        old_store.close()

    workbook = load_workbook(excel_path, data_only=False)
    worksheet = workbook["Offres"]
    worksheet["C2"] = f'=HYPERLINK("{source_url}{fragment}","Ouvrir")'
    worksheet["A2"] = '=B2="Postulé"'
    workbook.save(excel_path)
    workbook.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        (excel_path, csv_path, _raw_csv_path), total = export_job_formats(
            empty_store, export_dir, formats=("xlsx", "csv")
        )

        assert total == 1
        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet["A2"].value is False
        assert worksheet["C2"].value == f'=HYPERLINK("{source_url}","{source_url}")'
        assert "Ouvrir" not in pd.read_csv(csv_path).loc[0, "Lien"]
    finally:
        empty_store.close()


def test_export_preserves_existing_checkbox_for_matching_tracker_rows(tmp_path: Path) -> None:
    (excel_path, csv_path, _), total = _export_refreshed_linkedin_offer(tmp_path, "Postulé", True)

    assert total == 1
    assert bool(pd.read_csv(csv_path).loc[0, "Postulé"])
    worksheet = load_workbook(excel_path, data_only=False)["Offres"]
    assert worksheet["A2"].value is True
    assert (
        worksheet["C2"].value
        == '=HYPERLINK("https://linkedin.example/jobs/1?utm_source=refresh","https://linkedin.example/jobs/1?utm_source=refresh")'
    )


def test_export_does_not_retain_existing_rows_rejected_by_current_filters(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    old_store = JobStore(tmp_path / "old.duckdb")
    try:
        old_store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/alternance",
                title="Offre d’alternance / Community Manager (H/F)",
                company="Example",
                employment_type="Temps plein",
            )
        )
        export_job_formats(old_store, export_dir, formats=("xlsx", "csv"))
    finally:
        old_store.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        (excel_path, csv_path, _raw_csv_path), total = export_job_formats(
            empty_store,
            export_dir,
            formats=("xlsx", "csv"),
            include_terms=("community",),
            exclude_terms=("alternance",),
        )

        assert total == 0
        assert pd.read_csv(csv_path).empty
        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet.max_row == 1
        assert list(worksheet.tables.keys()) == []
    finally:
        empty_store.close()


def test_export_does_not_retain_existing_rows_rejected_by_location_scope(
    tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    export_dir = tmp_path / "exports"
    old_store = JobStore(tmp_path / "old.duckdb")
    try:
        old_store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/thiers",
                title="Example manager",
                company="Example",
                location="Thiers, Auvergne-Rhône-Alpes, France",
            )
        )
        export_job_formats(old_store, export_dir, formats=("xlsx", "csv"))
    finally:
        old_store.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        (_excel_path, csv_path, _raw_csv_path), total = export_job_formats(
            empty_store,
            export_dir,
            formats=("xlsx", "csv"),
            location_allowed=lambda location: location_is_in_scope(location, toulouse_scope),
        )

        assert total == 0
        assert pd.read_csv(csv_path).empty
    finally:
        empty_store.close()


def test_export_keeps_postule_boolean_when_status_column_is_omitted(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="hellowork",
                source_url="https://www.hellowork.com/fr-fr/emplois/1.html",
                direct_url="https://www.hellowork.com/fr-fr/emplois/1.html#postuler",
                title="Example manager",
                company="Example",
                location="Example City",
                description="Full-time example role with sample responsibilities.",
            )
        )

        (excel_path, _csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
            excel_columns=("Postulé", "Lien", "Intitulé", "Entreprise"),
        )

        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet["A2"].value is False
        assert worksheet["B2"].value == (
            '=HYPERLINK("https://www.hellowork.com/fr-fr/emplois/1.html",'
            '"https://www.hellowork.com/fr-fr/emplois/1.html")'
        )
    finally:
        store.close()


def test_purged_description_only_contract_cannot_return_from_old_tracker(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    offer = JobOffer(
        source="indeed",
        source_url="https://example.com/jobs/student",
        direct_url="https://example.com/apply/student",
        title="Assistant communication",
        company="Example",
        description="Type d'alternance : Apprentissage",
        employment_type="FULL_TIME",
    )
    export_dir = tmp_path / "exports"
    try:
        store.upsert_offer(offer)
        export_job_formats(store, export_dir, formats=("xlsx", "csv"), excel_columns=("Statut", "Lien", "Intitulé"))
        assert store.purge_excluded_offers(("alternance", "apprentissage")) == 1
    finally:
        store.close()
    # Reopening models the next scheduled run loading a stale remote workbook.
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert len(store.excluded_url_keys()) == 2
        (_, csv_path, _), total = export_job_formats(
            store, export_dir, formats=("xlsx", "csv"), include_terms=("communication",)
        )
        assert total == 0
        assert pd.read_csv(csv_path).empty
        store.upsert_offer(replace(offer, description="Paid permanent role", employment_type="CDI"))
        assert not store.excluded_url_keys()
        assert export_job_formats(store, export_dir, formats=("xlsx", "csv"), include_terms=("communication",))[1] == 1
    finally:
        store.close()


def test_export_preserves_status_when_canonical_source_changes_without_restoring_duplicate(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        offer = JobOffer(
            source="indeed",
            source_url="https://indeed.example/job/1",
            title="Communication",
            company="Example",
            employment_type="CDI",
        )
        store.upsert_offer(offer)
        columns = ("Statut", "Lien", "Intitulé")
        (path, _, _), _ = export_job_formats(
            store, tmp_path / "exports", formats=("xlsx", "csv"), excel_columns=columns
        )
        book = load_workbook(path)
        book["Offres"]["A2"] = "Postulé"
        book.save(path)
        store.upsert_offer(
            replace(
                offer,
                source="linkedin",
                source_url="https://linkedin.example/job/2",
                description="A richer description with the role's responsibilities.",
            )
        )
        (path, csv_path, _), total = export_job_formats(
            store, tmp_path / "exports", formats=("xlsx", "csv"), excel_columns=columns
        )
        assert total == 1
        row = pd.read_csv(csv_path).iloc[0]
        assert row["Statut"] == "Postulé"
        assert "linkedin.example" in row["Lien"]
    finally:
        store.close()


@pytest.mark.parametrize(
    ("primary", "secondary", "expected"), [("En Attente", "Postulé", "Postulé"), ("Postulé", "Refusé", "Refusé")]
)
def test_duplicate_status_conflicts_stay_visible_after_repeated_exports(
    tmp_path: Path, primary: str, secondary: str, expected: str
) -> None:
    from scrapper.export.jobs import _read_existing_tracker
    from scrapper.export.tracker import _merge_existing_tracker_rows

    columns = ["Statut", "Lien", "Intitulé", "À vérifier"]
    old = pd.DataFrame(
        [
            [primary, "https://example.com/a", "Communication", ""],
            [secondary, "https://example.com/b", "Communication", ""],
        ],
        columns=pd.Index(columns),
    )
    path = tmp_path / "jobs.csv"
    old.to_csv(path, index=False)
    current = old.iloc[:1].copy()
    aliases = {"url:example.com/a": {"url:example.com/a", "url:example.com/b"}}
    merged = _merge_existing_tracker_rows(
        current, _read_existing_tracker((tmp_path / "missing.xlsx", path)), current_link_aliases=aliases
    )
    assert len(merged) == 1
    assert merged.iloc[0]["Statut"] == expected
    assert merged.iloc[0]["À vérifier"] == f"Statuts divergents entre doublons : {primary} / {secondary}"
    merged.to_csv(path, index=False)
    repeated = _merge_existing_tracker_rows(
        current, _read_existing_tracker((tmp_path / "missing.xlsx", path)), current_link_aliases=aliases
    )
    assert repeated.iloc[0]["Statut"] == expected
    assert repeated.iloc[0]["À vérifier"] == merged.iloc[0]["À vérifier"]


def test_tracker_flags_offers_without_details(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        for job_id, contract in ((1, None), (2, "CDI")):
            store.upsert_offer(
                JobOffer(
                    source="glassdoor",
                    source_id=str(job_id),
                    source_url=f"https://glassdoor.example/jobs/{job_id}",
                    title="Communication",
                    company="Example",
                    location="Toulouse",
                    employment_type=contract,
                )
            )
        (_, csv_path, _), total = export_job_formats(
            store, tmp_path / "exports", formats=("xlsx", "csv"), excel_columns=("Lien", "Contrat", "À vérifier")
        )
    finally:
        store.close()

    rows = pd.read_csv(csv_path, keep_default_na=False).sort_values("Lien").reset_index(drop=True)
    assert total == 2
    assert rows["Contrat"].tolist() == ["", "CDI"]
    assert rows["À vérifier"].tolist() == ["Description et type de contrat indisponibles", ""]
