from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from scrapper.contracts import contract_evidence
from scrapper.export import export_job_formats
from scrapper.export.xlsx import read_xlsx_tracker
from scrapper.models import JobOffer
from scrapper.storage import JobStore


@pytest.mark.parametrize(
    ("metadata", "title", "description", "expected"),
    [
        ("FULL_TIME", "Example coordinator", "Type d'emploi : Temps plein, CDI", "CDI"),
        ("FULL_TIME", "Example coordinator", "Plus d'avantagesContrat : CDI - 35 h.", "CDI"),
        ("FULL_TIME", "Example coordinator", "Type de recrutement CDD de remplacement", "CDD"),
        ("FULL_TIME", "Example coordinator", "CDD 6 mois avec perspective d’évolution en CDI.", "CDD"),
        ("FULL_TIME", "CDD - Example coordinator", "", "CDD"),
        ("CDI - Contrat travail", "Example coordinator", "", "CDI"),
        ("TEMPORARY, FULL_TIME", "Example coordinator", "", "Temporaire"),
        ("Temps plein", "Example coordinator", "Vous encadrez les salariés en CDI.", ""),
        ("fulltime", "Example coordinator", "Agence spécialisée dans le recrutement en CDI.", ""),
        ("CDI", "Example coordinator", "Type d'emploi : Emploi permanent - vacance d'emploi.", "Emploi permanent"),
        ("PART_TIME", "Mission freelance - Example coordinator", "", "Indépendant"),
        ("Temps plein", "Chargé de communication", "Nous recherchons un(e) chargé(e) de communication en CDI.", "CDI"),
        ("Temps plein", "Chargé de communication", "L'ESTBB recrute un(e) chargé(e) à temps complet en CDD.", "CDD"),
        ("Temps plein", "Chargé de communication", "Nous recherchons un profil avec une évolution en CDI.", ""),
        ("Temps plein", "Chargé de communication", "Nous recrutons un profil en CDD ou en CDI selon expérience.", ""),
        ("CDD", "Service Civique - Chargé de communication", "", "Service civique"),
        (
            "Temps plein",
            "Marketing Executive",
            "Location: Toulouse – Contract type: CDI – Salary: based on experience",
            "CDI",
        ),
        ("Temps plein", "Marketing Executive", "Contract: Permanent (CDI)", "CDI"),
        ("Temps plein", "Chef de projet", "Poste basé à Toulouse (31) en CDI, à pourvoir dès que possible.", "CDI"),
        ("Temps plein", "Chef de projet", "Type : CDI Expérience : 4 ans", "CDI"),
        ("Temps plein", "Chargé de communication", "CDD de remplacement dans le cadre d'un congé maternité.", "CDD"),
    ],
)
def test_contract_uses_offer_type_over_schedule(metadata: str, title: str, description: str, expected: str) -> None:
    offer = {"employment_type": metadata, "title": title, "description": description}
    assert contract_evidence(offer.get)[1] == expected


def test_export_recovers_salary_and_contract_from_matched_offer(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "offers.duckdb", source_priority={"indeed": 0})
    offer = JobOffer(
        source="indeed",
        source_url="https://indeed.example/jobs/primary",
        title="Example coordinator",
        company="Example employer",
        location="Toulouse",
        employment_type="fulltime",
        description="The main copy has a lengthy description of the responsibilities but no salary.",
    )
    try:
        store.upsert_offer(offer)
        store.upsert_offer(
            replace(
                offer,
                source="france_travail",
                source_url="https://candidat.francetravail.fr/offres/recherche/detail/example",
                description="Coordonnez les publications.",
                employment_type="CDD - 6 Mois - Contrat travail",
                salary="Annuel de 35000.0 Euros à 38000.0 Euros",
            )
        )
        store.upsert_offer(
            replace(
                offer,
                source="hellowork",
                source_url="https://www.hellowork.com/fr-fr/emplois/12345.html",
                raw={"json_ld": {"jobLocationType": "TELECOMMUTE"}},
            )
        )
        columns = ("Lien", "Contrat", "Salaire min (€ annuel)", "Salaire max (€ annuel)", "Télétravail")
        (path,), count = export_job_formats(store, tmp_path / "exports", columns, formats=("xlsx",))
        assert count == 1
        result = read_xlsx_tracker(path).iloc[0]
        assert str(offer.source_url) in str(result["Lien"])
        assert result["Contrat"] == "CDD"
        assert result["Salaire min (€ annuel)"] == 35000
        assert result["Salaire max (€ annuel)"] == 38000
        assert result["Télétravail"] == "Oui"
        assert set(store.raw_dataframe()["employment_type"]) == {"fulltime", "CDD - 6 Mois - Contrat travail"}
    finally:
        store.close()
