from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Never

import pandas as pd
import pytest
from openpyxl import load_workbook

from scrapper import export
from scrapper.config import SearchConfig
from scrapper.export import _date_fr, _date_value, _review_note, _salary_bounds, export_jobs
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore


@pytest.mark.parametrize("stage", ["format", "publish"])
def test_failed_export_keeps_last_good_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(source="board", source_url="https://example.com/1", title="Analyst", company="Example")
        )
        *outputs, _ = export_jobs(store, tmp_path / "exports")
        last_good = {path: path.read_bytes() for path in outputs}
        store.upsert_offer(
            JobOffer(source="board", source_url="https://example.com/2", title="Engineer", company="Example")
        )

        def fail(*args: object) -> Never:
            raise OSError("Export interrupted")

        if stage == "format":
            monkeypatch.setattr(export, "_format_jobs_sheet", fail)
        else:
            replace = Path.replace

            def fail_on_workbook(staged: Path, output: Path) -> Path:
                if output.suffix == ".xlsx":
                    fail()
                return replace(staged, output)

            monkeypatch.setattr(Path, "replace", fail_on_workbook)

        with pytest.raises(OSError, match="Export interrupted"):
            export_jobs(store, tmp_path / "exports")
        assert {path: path.read_bytes() for path in outputs} == last_good
    finally:
        store.close()


@pytest.fixture
def hellowork_offer() -> JobOffer:
    return JobOffer(
        source="hellowork",
        source_url="https://www.hellowork.com/fr-fr/emplois/1.html",
        direct_url="https://apply.example.com/1",
        title="Example manager",
        company="Example",
        location="Example City",
        description="Full-time example role with sample responsibilities.",
    )


def test_export_creates_localized_tracker_with_links_filters_and_annual_salary(
    tmp_path: Path, hellowork_offer: JobOffer
) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            replace(
                hellowork_offer,
                employment_type="CDI",
                date_posted="2026-05-29T10:11:12+00:00",
                salary=str(
                    {
                        "@type": "MonetaryAmount",
                        "currency": "EUR",
                        "value": {
                            "@type": "QuantitativeValue",
                            "minValue": 3000,
                            "maxValue": 3600,
                            "unitText": "MONTH",
                        },
                    }
                ),
                raw={
                    "json_ld": {
                        "baseSalary": {
                            "@type": "MonetaryAmount",
                            "currency": "EUR",
                            "value": {
                                "@type": "QuantitativeValue",
                                "minValue": 3000,
                                "maxValue": 3600,
                                "unitText": "MONTH",
                            },
                        }
                    }
                },
            )
        )

        excel_path, csv_path, raw_csv_path, total = export_jobs(store, tmp_path / "exports")

        assert total == 1
        assert csv_path.exists()

        workbook = load_workbook(excel_path, data_only=False)
        worksheet = workbook["Offres"]
        assert workbook.sheetnames == ["Offres", "Données brutes"]
        raw_headers = [cell.value for cell in workbook["Données brutes"][1]]
        assert raw_headers == pd.read_csv(raw_csv_path, nrows=0).columns.tolist()
        assert {"company_description", "raw_json", "currency"}.isdisjoint(raw_headers)
        assert list(worksheet.tables.keys()) == ["TableOffres"]
        assert worksheet.tables["TableOffres"].ref == "A1:N2"
        assert worksheet.tables["TableOffres"].autoFilter.ref == "A1:N2"
        assert worksheet.auto_filter.ref is None
        removed_headers = {
            "Statut",
            "Postuler",
            "Offre source",
            "Autres liens",
            "Source principale",
            "Doublons",
            "Description",
            "URL entreprise",
            "Email",
            "Zone recherchée",
            "Score complétude",
            "Vu le",
            "Dernière vue",
        }
        assert removed_headers.isdisjoint(
            raw_header for cell in worksheet[1] if isinstance(raw_header := cell.value, str)
        )
        assert worksheet["A2"].value is False
        assert worksheet["B2"].value is False
        assert worksheet["C2"].value == '=HYPERLINK("https://apply.example.com/1","https://apply.example.com/1")'
        assert worksheet["I2"].value == 36000
        assert worksheet["J2"].value == 43200
        assert worksheet["K2"].value == "36 000-43 200 €"
        published_value = worksheet["L2"].value
        assert isinstance(published_value, datetime)
        assert published_value.date() == date(2026, 5, 29)
        assert worksheet["L2"].number_format == "dd/mm/yyyy"
    finally:
        store.close()


def test_export_handles_empty_store_with_tracker_headers(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        excel_path, csv_path, raw_csv_path, total = export_jobs(store, tmp_path / "exports")

        assert total == 0
        assert csv_path.exists()
        assert raw_csv_path.exists()

        workbook = load_workbook(excel_path, data_only=False)
        worksheet = workbook["Offres"]
        assert workbook.sheetnames == ["Offres", "Données brutes"]
        assert worksheet.max_row == 1
        assert worksheet["A1"].value == "Postulé"
        assert worksheet["B1"].value == "Rejeté"
        assert list(worksheet.tables.keys()) == []
    finally:
        store.close()


def test_export_orders_rows_by_published_date_descending_with_blanks_last(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        for title, date_posted in (
            ("Older example role", "2026-05-01"),
            ("Undated example role", None),
            ("Newer example role", "2026-06-01"),
        ):
            store.upsert_offer(
                JobOffer(
                    source="linkedin",
                    source_url=f"https://linkedin.example/jobs/{title.replace(' ', '-').lower()}",
                    title=title,
                    company="Example",
                    location="Toulouse",
                    description="Full-time example role in communication.",
                    date_posted=date_posted,
                )
            )

        excel_path, _, _, total = export_jobs(store, tmp_path / "exports")

        workbook = load_workbook(excel_path, data_only=False)
        worksheet = workbook["Offres"]
        titles = [worksheet[f"D{row}"].value for row in range(2, 5)]

        assert total == 3
        assert titles == ["Newer example role", "Older example role", "Undated example role"]
    finally:
        store.close()


def test_export_uses_configured_excel_columns(tmp_path: Path, hellowork_offer: JobOffer) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(hellowork_offer)

        excel_path, csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
            excel_columns=("Intitulé", "Entreprise", "Lien"),
        )

        workbook = load_workbook(excel_path, data_only=False)
        worksheet = workbook["Offres"]
        assert worksheet.tables["TableOffres"].ref == "A1:C2"
        assert worksheet.tables["TableOffres"].autoFilter.ref == "A1:C2"
        assert [cell.value for cell in worksheet[1]] == ["Intitulé", "Entreprise", "Lien"]
        assert worksheet["A2"].value == "Example manager"
        assert worksheet["B2"].value == "Example"
        assert worksheet["C2"].value == '=HYPERLINK("https://apply.example.com/1","https://apply.example.com/1")'
        assert pd.read_csv(csv_path).columns.tolist() == ["Intitulé", "Entreprise", "Lien"]
    finally:
        store.close()


def test_export_can_disable_direct_links_by_source(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="jobijoba",
                source_url="https://www.jobijoba.com/fr/annonce/54/1",
                direct_url="https://apply.example.com/1",
                title="Example manager",
                company="Example",
                location="Example City",
                description="Full-time example role with sample responsibilities.",
            )
        )

        excel_path, _csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
            excel_columns=("Lien", "Postuler", "Offre source"),
            direct_links_enabled_by_source={"jobijoba": False},
        )

        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        expected = '=HYPERLINK("https://www.jobijoba.com/fr/annonce/54/1","https://www.jobijoba.com/fr/annonce/54/1")'
        assert worksheet["A2"].value == expected
        assert worksheet["B2"].value == expected
        assert worksheet["C2"].value == expected
    finally:
        store.close()


def test_export_adds_status_dropdown_validation(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="hellowork",
                source_url="https://www.hellowork.com/fr-fr/emplois/1.html",
                title="Example manager",
                company="Example",
                location="Example City",
            )
        )

        excel_path, _csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
            excel_columns=("Statut", "Lien", "Intitulé", "Entreprise"),
        )

        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        validations = list(worksheet.data_validations.dataValidation)
        assert len(validations) == 1
        assert validations[0].type == "list"
        assert validations[0].formula1 == '"En Attente,Postulé,Refusé"'
        assert "A2:A1048576" in str(validations[0].sqref)
        assert worksheet["A2"].value == "En Attente"
        color_ranges = list(worksheet.conditional_formatting)
        assert len(color_ranges) == 1
        assert str(color_ranges[0].sqref) == "A2"
        rules = worksheet.conditional_formatting[color_ranges[0]]
        assert [rule.formula for rule in rules] == [['"En Attente"'], ['"Postulé"'], ['"Refusé"']]
        assert all(rule.type == "cellIs" and rule.operator == "equal" and rule.dxf for rule in rules)
    finally:
        store.close()


def _export_refreshed_linkedin_offer(
    tmp_path: Path, header: str, value: str | bool, excel_columns: tuple[str, ...] | None = None
) -> tuple[Path, Path, Path, int]:
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
        excel_path, _, _, _ = export_jobs(store, export_dir, excel_columns=(header, "Lien", "Intitulé", "Entreprise"))
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
        return export_jobs(refreshed_store, export_dir, excel_columns=excel_columns)
    finally:
        refreshed_store.close()


@pytest.mark.parametrize("status_header", ["Statut", "Postulé"])
@pytest.mark.parametrize("status", ["Postulé", "Refusé"])
def test_export_preserves_existing_status_for_matching_tracker_rows(
    tmp_path: Path, status_header: str, status: str
) -> None:
    excel_path, csv_path, _, total = _export_refreshed_linkedin_offer(
        tmp_path, status_header, status, excel_columns=("Statut", "Lien", "Intitulé", "Entreprise")
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
    excel_path, csv_path, _, total = _export_refreshed_linkedin_offer(
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
        export_jobs(old_store, export_dir, excel_columns=("Statut", "Lien", "Intitulé", "Entreprise"))
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

        excel_path, csv_path, _raw_csv_path, total = export_jobs(
            new_store,
            export_dir,
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
        excel_path, _csv_path, _raw_csv_path, _total = export_jobs(old_store, export_dir)
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
        excel_path, csv_path, _raw_csv_path, total = export_jobs(empty_store, export_dir)

        assert total == 1
        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet["A2"].value is False
        assert worksheet["C2"].value == f'=HYPERLINK("{source_url}","{source_url}")'
        assert "Ouvrir" not in pd.read_csv(csv_path).loc[0, "Lien"]
    finally:
        empty_store.close()


def test_export_preserves_existing_checkbox_for_matching_tracker_rows(tmp_path: Path) -> None:
    excel_path, csv_path, _, total = _export_refreshed_linkedin_offer(tmp_path, "Postulé", True)

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
        export_jobs(old_store, export_dir)
    finally:
        old_store.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        excel_path, csv_path, _raw_csv_path, total = export_jobs(
            empty_store,
            export_dir,
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


def test_export_does_not_retain_existing_rows_rejected_by_location_scope(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    config = SearchConfig()
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
        export_jobs(old_store, export_dir)
    finally:
        old_store.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        _excel_path, csv_path, _raw_csv_path, total = export_jobs(
            empty_store,
            export_dir,
            location_allowed=lambda location: location_is_in_scope(location, config.location_scope),
        )

        assert total == 0
        assert pd.read_csv(csv_path).empty
    finally:
        empty_store.close()


def test_export_does_not_retain_meteojob_placeholder_rows(tmp_path: Path) -> None:
    export_dir = tmp_path / "exports"
    old_store = JobStore(tmp_path / "old.duckdb")
    try:
        old_store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/valid",
                title="Valid example manager",
                company="Example",
                location="Toulouse",
                date_posted="2026-07-01",
            )
        )
        old_store.upsert_offer(
            JobOffer(
                source="meteojob",
                source_url="https://www.meteojob.com/jobs/55275233",
                title="Emploi Chargé de communication à Cugnaux",
                company="Meteojob",
                location="Cugnaux",
                date_posted=None,
            )
        )
        export_jobs(
            old_store,
            export_dir,
            excel_columns=("Publié le", "Lien", "Intitulé", "Entreprise"),
        )
    finally:
        old_store.close()

    empty_store = JobStore(tmp_path / "empty.duckdb")
    try:
        excel_path, csv_path, _raw_csv_path, total = export_jobs(
            empty_store,
            export_dir,
            excel_columns=("Publié le", "Lien", "Intitulé", "Entreprise"),
        )

        assert total == 1
        assert pd.read_csv(csv_path)["Intitulé"].to_list() == ["Valid example manager"]
        worksheet = load_workbook(excel_path, data_only=False)["Offres"]
        assert worksheet.max_row == 2
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

        excel_path, _csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
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


@pytest.mark.parametrize(
    ("repairs_enabled", "expected_title", "expected_company", "expected_location"),
    [
        (True, "Chargé de communication (F/H)", "", "Blagnac"),
        (False, "208WKXK Chargé de communication (F/H)", "31 - Blagnac - Localiser avec Mappy", "Toulouse"),
    ],
    ids=["default-repairs", "repairs-disabled-for-source"],
)
def test_export_repairs_france_travail_tracker_fields(
    tmp_path: Path, repairs_enabled: bool, expected_title: str, expected_company: str, expected_location: str
) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="france_travail",
                source_url="https://candidat.francetravail.fr/offres/recherche/detail/208WKXK",
                direct_url="https://candidat.francetravail.fr/offres/recherche/detail/208WKXK",
                title="208WKXK Chargé de communication (F/H)",
                company="31 - Blagnac - Localiser avec Mappy",
                location="Toulouse",
                description="Votre objectif principal sera de mettre en œuvre la stratégie de communication.",
            )
        )

        _excel_path, csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
            excel_columns=("Intitulé", "Entreprise", "Lieu"),
            repair_fields_by_source={} if repairs_enabled else {"france_travail": False},
        )

        frame = pd.read_csv(csv_path).fillna("")
        assert frame.iloc[0].to_dict() == {
            "Intitulé": expected_title,
            "Entreprise": expected_company,
            "Lieu": expected_location,
        }
    finally:
        store.close()


def test_town_cleanup_handles_other_regions_and_street_addresses() -> None:
    assert export._town_fr("Paris 9e, Île-de-France") == "Paris"
    assert export._town_fr("Marseille 2e Arrondissement, Provence-Alpes-Côte d'Azur") == "Marseille"
    assert export._town_fr("1 rue de la République, Beauzelle (31)") == "Beauzelle"


def test_export_normalizes_location_to_town_for_tracker_display(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/1",
                title="Chargé communication",
                company="Example",
                location="Toulouse, Occitanie, France",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/2",
                title="Responsable communication",
                company="Example",
                location="Cornebarrieu, Occitanie, FR",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="france_travail",
                source_url="https://candidat.francetravail.fr/offres/recherche/detail/1",
                title="Community manager",
                company="Example",
                location="Chargé(e) de Communication - Relations Presse (H/F) 31 - Toulouse 2e Arrondissement",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="jobijoba",
                source_url="https://www.jobijoba.com/fr/annonce/54/1",
                title="Content manager",
                company="Example",
                location="Toulouse 9e",
            )
        )

        _excel_path, csv_path, _raw_csv_path, _total = export_jobs(
            store,
            tmp_path / "exports",
            excel_columns=("Intitulé", "Entreprise", "Lieu"),
        )

        frame = pd.read_csv(csv_path).sort_values("Intitulé").reset_index(drop=True)
        assert frame["Lieu"].tolist() == ["Toulouse", "Toulouse", "Toulouse", "Cornebarrieu"]
    finally:
        store.close()


def test_export_rejects_unknown_excel_columns(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        with pytest.raises(ValueError, match="Unknown Excel column"):
            export_jobs(store, tmp_path / "exports", excel_columns=("Intitulé", "Bad column"))
    finally:
        store.close()


def test_salary_bounds_leave_untrusted_non_annual_units_empty() -> None:
    row = pd.Series(
        {
            "min_amount": None,
            "max_amount": None,
            "salary": {
                "@type": "MonetaryAmount",
                "currency": "EUR",
                "value": {
                    "@type": "QuantitativeValue",
                    "minValue": 20,
                    "maxValue": 30,
                    "unitText": "HOUR",
                },
            },
            "raw_json": None,
        }
    )

    assert _salary_bounds(row) == (None, None)


def test_date_fr_formats_iso_natural_language_and_relative_dates() -> None:
    today = date(2026, 6, 4)

    assert _date_fr("2026-05-29T10:11:12+00:00", today=today) == "29/05/2026"
    assert _date_fr("Publié le 28 mai 2026", today=today) == "28/05/2026"
    assert _date_fr("29 mai Description de l'offre", today=today) == "29/05/2026"
    assert _date_fr("24 avril 2026 Le Syndic", today=today) == "24/04/2026"
    assert _date_fr("Il y a 10 h Description", today=today) == "04/06/2026"
    assert _date_fr("Il y a 2 jours", today=today) == "02/06/2026"
    assert _date_value("29/05/2026", today=today) == date(2026, 5, 29)
    assert _date_value("Il y a 20 h", today=today) == date(2026, 6, 3)
    assert _date_value("Il y a 2 semaines", today=today) == date(2026, 5, 21)
    assert _date_value("31 décembre", today=date(2026, 1, 2)) == date(2025, 12, 31)


@pytest.mark.parametrize(
    ("minimum", "maximum", "unit", "currency", "expected"),
    [
        (2400, 2500, "monthly", "EUR", (28800, 30000)),
        (2900, 2900, "MONTH", "EUR", (34800, 34800)),
        (36000, 44000, "yearly", "EUR", (36000, 44000)),
        (40, 56, "hourly", "EUR", (None, None)),
        (300, 400, "daily", "EUR", (None, None)),
        (2400, 2500, None, "EUR", (None, None)),
        (40000, 45000, "yearly", "USD", (None, None)),
        (3000, 2000, "monthly", "EUR", (None, None)),
        (-100, 2000, "monthly", "EUR", (None, None)),
        (None, 3000, "monthly", "EUR", (None, 36000)),
        (3000, None, "monthly", "EUR", (36000, None)),
    ],
)
def test_audit_salary_units_and_unreliable_ranges(
    minimum: int | None, maximum: int | None, unit: str | None, currency: str, expected: tuple[int | None, int | None]
) -> None:
    assert (
        _salary_bounds(
            pd.Series(
                {
                    "min_amount": minimum,
                    "max_amount": maximum,
                    "interval": unit,
                    "currency": currency,
                }
            )
        )
        == expected
    )


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
        export_jobs(store, export_dir, excel_columns=("Statut", "Lien", "Intitulé"))
        assert store.purge_excluded_offers(("alternance", "apprentissage")) == 1
    finally:
        store.close()
    # Reopening models the next scheduled run loading a stale remote workbook.
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert len(store.excluded_url_keys()) == 2
        _, csv_path, _, total = export_jobs(store, export_dir, include_terms=("communication",))
        assert total == 0
        assert pd.read_csv(csv_path).empty
        store.upsert_offer(replace(offer, description="Paid permanent role", employment_type="CDI"))
        assert not store.excluded_url_keys()
        assert export_jobs(store, export_dir, include_terms=("communication",))[3] == 1
    finally:
        store.close()


def test_export_filters_current_titles_and_flags_uncertain_low_pay_without_losing_status(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    export_dir = tmp_path / "exports"
    columns = ("Statut", "Lien", "Intitulé", "Salaire min (€ annuel)", "À vérifier")
    try:
        for title, slug, amount in [
            ("Assistant communication", "assistant", 630),
            ("Développeur communication", "developer", 3000),
        ]:
            store.upsert_offer(
                JobOffer(
                    source="indeed",
                    source_url=f"https://example.com/jobs/{slug}",
                    title=title,
                    company="Example",
                    min_amount=amount,
                    max_amount=amount,
                    interval="monthly",
                    currency="EUR",
                )
            )
        path, _, _, _ = export_jobs(store, export_dir, excel_columns=columns)
        book = load_workbook(path)
        offers = book["Offres"]
        for row in range(2, offers.max_row + 1):
            offers.cell(row, 1, "Postulé")
        book.save(path)
        path, csv_path, _, total = export_jobs(
            store,
            export_dir,
            excel_columns=columns,
            include_terms=("communication",),
            excluded_title_patterns=(r"^developpeur\b",),
        )
        assert total == 1
        row = pd.read_csv(csv_path).iloc[0]
        assert row["Intitulé"] == "Assistant communication"
        assert row["Statut"] == "Postulé"
        assert row["Salaire min (€ annuel)"] == 7560
        assert "vérifier stage/alternance" in row["À vérifier"]
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
        path, _, _, _ = export_jobs(store, tmp_path / "exports", excel_columns=columns)
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
        path, csv_path, _, total = export_jobs(store, tmp_path / "exports", excel_columns=columns)
        assert total == 1
        row = pd.read_csv(csv_path).iloc[0]
        assert row["Statut"] == "Postulé"
        assert "linkedin.example" in row["Lien"]
    finally:
        store.close()


def test_export_checks_contract_evidence_from_noncanonical_copy(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb", source_priority={"indeed": 0, "glassdoor": 10})
    try:
        for source, description in [("indeed", None), ("glassdoor", "Lieu du stage : Toulouse")]:
            store.upsert_offer(
                JobOffer(
                    source=source,
                    source_url=f"https://{source}.example/jobs/1",
                    title="Responsable Marketing",
                    company="Example",
                    description=description,
                )
            )
        assert store.canonical_dataframe().iloc[0]["best_source"] == "indeed"
        assert export_jobs(store, tmp_path / "exports", exclude_terms=("stage",))[3] == 0
    finally:
        store.close()


def test_review_notes_distinguish_teaching_recruitment_from_mentoring() -> None:
    assert "formation" in _review_note(pd.Series({"description": "Nous recrutons des formateurs/trices indépendants."}))
    assert not _review_note(
        pd.Series({"description": "Vous encadrez les stagiaires et préparez les contenus marketing."})
    )


def test_duplicate_status_conflicts_stay_visible_after_repeated_exports(tmp_path: Path) -> None:
    from scrapper.export import _merge_existing_tracker_rows

    columns = ["Statut", "Lien", "Intitulé", "À vérifier"]
    old = pd.DataFrame(
        [
            ["En Attente", "https://example.com/a", "Communication", ""],
            ["Postulé", "https://example.com/b", "Communication", ""],
        ],
        columns=pd.Index(columns),
    )
    path = tmp_path / "jobs.csv"
    old.to_csv(path, index=False)
    current = old.iloc[:1].copy()
    aliases = {"url:example.com/a": {"url:example.com/a", "url:example.com/b"}}
    merged = _merge_existing_tracker_rows(current, tmp_path / "missing.xlsx", path, current_link_aliases=aliases)
    assert len(merged) == 1
    assert merged.iloc[0]["Statut"] == "En Attente"
    assert merged.iloc[0]["À vérifier"] == "Statuts divergents entre doublons : En Attente / Postulé"
    merged.to_csv(path, index=False)
    repeated = _merge_existing_tracker_rows(current, tmp_path / "missing.xlsx", path, current_link_aliases=aliases)
    assert repeated.iloc[0]["À vérifier"] == merged.iloc[0]["À vérifier"]
