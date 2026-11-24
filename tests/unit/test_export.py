from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import Never

import pandas as pd
import pytest
from openpyxl import load_workbook

from jobsfr.export import export_job_formats, tracker, xlsx
from jobsfr.export.tracker import _date_fr, _date_value
from jobsfr.offers.filters import ListingFilters, RegexRule, TextFilter
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


@pytest.mark.parametrize("stage", ["format", "publish"])
def test_failed_export_keeps_last_good_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(source="board", source_url="https://example.com/1", title="Analyst", company="Example")
        )
        outputs, _ = export_job_formats(store, tmp_path / "exports", formats=("xlsx", "csv"))
        last_good = {path: path.read_bytes() for path in outputs}
        store.upsert_offer(
            JobOffer(source="board", source_url="https://example.com/2", title="Engineer", company="Example")
        )

        def fail(*args: object) -> Never:
            raise OSError("Export interrupted")

        if stage == "format":
            monkeypatch.setattr(xlsx, "_format_jobs_sheet", fail)
        else:
            replace = Path.replace

            def fail_on_raw_csv(staged: Path, output: Path) -> Path:
                if output.name == "jobs_raw.csv":
                    fail()
                return replace(staged, output)

            monkeypatch.setattr(Path, "replace", fail_on_raw_csv)

        with pytest.raises(OSError, match="Export interrupted"):
            export_job_formats(store, tmp_path / "exports", formats=("xlsx", "csv"))
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

        (excel_path, csv_path, raw_csv_path), total = export_job_formats(
            store, tmp_path / "exports", formats=("xlsx", "csv")
        )

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
        (excel_path, csv_path, raw_csv_path), total = export_job_formats(
            store, tmp_path / "exports", formats=("xlsx", "csv")
        )

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

        (excel_path, _, _), total = export_job_formats(store, tmp_path / "exports", formats=("xlsx", "csv"))

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

        (excel_path, csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
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

        (excel_path, _csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
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


@pytest.mark.parametrize("missing_direct_url", [float("nan"), pd.NA])
def test_tracker_link_falls_back_to_source_url_for_missing_direct_url(missing_direct_url: object) -> None:
    source_url = "https://example.com/jobs/1"
    row = pd.Series({"best_source": "example", "best_source_url": source_url, "best_direct_url": missing_direct_url})

    assert tracker._tracker_apply_link(row, {}) == source_url


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

        (excel_path, _csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
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

        (_excel_path, csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
            excel_columns=("Intitulé", "Entreprise", "Lieu"),
            repair_fields_by_source={} if repairs_enabled else {"france_travail": False},
        )

        frame = pd.read_csv(csv_path, keep_default_na=False)
        assert frame.iloc[0].to_dict() == {
            "Intitulé": expected_title,
            "Entreprise": expected_company,
            "Lieu": expected_location,
        }
    finally:
        store.close()


def test_town_cleanup_handles_other_regions_and_street_addresses() -> None:
    assert tracker._town_fr("Paris 9e, Île-de-France") == "Paris"
    assert tracker._town_fr("Marseille 2e Arrondissement, Provence-Alpes-Côte d'Azur") == "Marseille"
    assert tracker._town_fr("1 rue de la République, Beauzelle (31)") == "Beauzelle"


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
                location="Chargé(e) de Communication - Relations Presse (H/F) 31 - Toulouse",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="jobijoba",
                source_url="https://www.jobijoba.com/fr/annonce/54/1",
                title="Content manager",
                company="Example",
                location="Marseille 9e",
            )
        )

        (_excel_path, csv_path, _raw_csv_path), _total = export_job_formats(
            store,
            tmp_path / "exports",
            formats=("xlsx", "csv"),
            excel_columns=("Intitulé", "Entreprise", "Lieu"),
        )

        frame = pd.read_csv(csv_path).sort_values("Intitulé").reset_index(drop=True)
        assert frame["Lieu"].tolist() == ["Toulouse", "Toulouse", "Marseille", "Cornebarrieu"]
    finally:
        store.close()


def test_export_rejects_unknown_excel_columns(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        with pytest.raises(ValueError, match="Unknown Excel column"):
            export_job_formats(
                store, tmp_path / "exports", formats=("xlsx", "csv"), excel_columns=("Intitulé", "Bad column")
            )
    finally:
        store.close()


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
        (path, _, _), _ = export_job_formats(store, export_dir, formats=("xlsx", "csv"), excel_columns=columns)
        book = load_workbook(path)
        offers = book["Offres"]
        for row in range(2, offers.max_row + 1):
            offers.cell(row, 1, "Postulé")
        book.save(path)
        (path, csv_path, _), total = export_job_formats(
            store,
            export_dir,
            formats=("xlsx", "csv"),
            excel_columns=columns,
            filters=ListingFilters(
                title=TextFilter(include=("communication",), exclude=(RegexRule(regex=r"^developpeur\b"),))
            ),
        )
        assert total == 1
        row = pd.read_csv(csv_path).iloc[0]
        assert row["Intitulé"] == "Assistant communication"
        assert row["Statut"] == "Postulé"
        assert row["Salaire min (€ annuel)"] == 7560
        assert "vérifier stage/alternance" in row["À vérifier"]
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
        assert export_job_formats(store, tmp_path / "exports", formats=("xlsx", "csv"))[1] == 0
    finally:
        store.close()
