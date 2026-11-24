from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from hypothesis import given
from hypothesis import strategies as st
from openpyxl import load_workbook

from jobsfr.export import export_job_formats
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore
from jobsfr.storage.dedupe import build_group_keys

_DESCRIPTION = (
    "Vous piloterez le calendrier éditorial du service culturel, étudierez les retours des visiteurs, "
    "préparerez les campagnes de communication à destination des associations et organiserez les ateliers "
    "de production avec les graphistes. Vous coordonnerez la livraison des brochures et des supports accessibles, "
    "animerez les points hebdomadaires avec les partenaires, puis présenterez les résultats au conseil. "
    "Le service travaille également sur des expositions itinérantes et publie un programme trimestriel. "
    "Vous améliorerez les tableaux de suivi des publications et accompagnerez les partenaires lors des événements publics."
)
_BASE = JobOffer(
    source="indeed",
    source_url="https://fr.indeed.com/viewjob?jk=synthetic-left",
    title="Responsable de communication du service culturel",
    company="Atelier Indigo",
    location="Toulouse",
    date_posted="2026-11-02",
    description=_DESCRIPTION,
)
_VARIANT = replace(
    _BASE,
    source="linkedin",
    source_url="https://www.linkedin.com/jobs/view/123456",
)
_PUBLIC = replace(
    _BASE,
    company="La Régie du Patrimoine",
    title="2 GRAPHISTES (F/H) Direction des publications culturelles",
)
_SUMMARY = replace(
    _PUBLIC,
    source="jobijoba",
    source_url="https://www.jobijoba.com/fr/annonce/54/synthetic-left",
    direct_url="https://www.jobijoba.com/fr/redirect/offer/54/synthetic-left",
    company="Régie du Patrimoine",
    title="2 Graphistes Direction des Publications Culturelles H/F",
    date_posted="2026-11-02T08:10:00Z",
    description=(
        "Détail du poste Employeur : RÉGIE DU PATRIMOINE Métier(s) : Graphiste "
        "Date de publication : du 02/11/2026 au 02/12/2026 Date limite de candidature : 02/12/2026 "
        "Type d'emploi : Emploi permanent - vacance d'emploi Nombre de postes : 2 Lieu de travail : Toulouse"
    ),
)


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        pytest.param(_PUBLIC, _SUMMARY, [2], id="public-vacancy-summary-omits-employer-article"),
        pytest.param(_PUBLIC, replace(_SUMMARY, date_posted="2026-10-02"), [1, 1], id="summary-on-different-day"),
        pytest.param(
            _SUMMARY,
            replace(
                _SUMMARY,
                source_url="https://www.jobijoba.com/fr/annonce/54/synthetic-right",
                direct_url="https://www.jobijoba.com/fr/redirect/offer/54/synthetic-right",
                description=(_SUMMARY.description or "").replace(" Métier(s)", "Métier(s)").replace(" Type", "Type"),
            ),
            [2],
            id="same-board-redirects-and-concatenated-markup",
        ),
        pytest.param(
            replace(_BASE, location="Ville-Exemple 9e Arrondissement, France"),
            replace(_VARIANT, location="Ville-Exemple"),
            [2],
            id="arrondissement-and-city",
        ),
        pytest.param(
            replace(_BASE, location="St.-Orens-de-Gameville (31)"),
            replace(_VARIANT, location="Saint-Orens-de-Gameville"),
            [2],
            id="generic-saint-abbreviation",
        ),
        pytest.param(_BASE, replace(_VARIANT, location="Colomiers"), [1, 1], id="different-work-cities"),
        pytest.param(
            _BASE,
            replace(
                _VARIANT,
                company="Atelier Indigo SAS",
                title="Responsable de communication du service culturrel (H/F/N)",
                description="Présentation de l'entreprise. " + _DESCRIPTION,
            ),
            [2],
            id="title-typo-legal-suffix-and-description-wrapper",
        ),
        pytest.param(
            _BASE,
            replace(_VARIANT, company="Atelier Indigo Partners", description=_DESCRIPTION + " Poste à temps complet."),
            [2],
            id="employer-brand-variant-with-strong-text-evidence",
        ),
        pytest.param(
            replace(_BASE, company="Université des Arts 3"),
            replace(_VARIANT, company="Université des Arts (III)"),
            [2],
            id="equivalent-numeric-and-roman-employer-suffix",
        ),
        pytest.param(
            replace(_BASE, company="Université des Arts 3"),
            replace(_VARIANT, company="Université des Arts (II)"),
            [1, 1],
            id="different-numbered-employers",
        ),
        pytest.param(
            _BASE,
            replace(_VARIANT, company="Enseigne non disponible", description=_DESCRIPTION + " Poste à temps complet."),
            [2],
            id="anonymous-copy-with-strong-text-evidence",
        ),
        pytest.param(_BASE, replace(_VARIANT, company="Atelier pourpre"), [1, 1], id="unrelated-employers"),
        pytest.param(
            replace(_BASE, description=None),
            replace(_VARIANT, title="Responsable des archives", company="Atelier Azur", description=None),
            [1, 1],
            id="missing-description-never-becomes-a-shared-key",
        ),
        pytest.param(
            replace(_BASE, company="Enseigne non disponible"),
            replace(_VARIANT, company="Enseigne non disponible", description="Coordonnez les campagnes de presse."),
            [1, 1],
            id="same-title-and-placeholder-employer-with-different-duties",
        ),
        pytest.param(
            replace(_BASE, direct_url="https://jobs.implid.com/fr/annonce/4712345-original-slug"),
            replace(
                _VARIANT,
                direct_url="https://jobs.implid.com/fr/annonce/4712345-revised-slug?utm_source=example",
                title="Titre réécrit",
            ),
            [2],
            id="same-employer-id-on-different-slugs",
        ),
        pytest.param(
            replace(_BASE, direct_url="https://jobs.implid.com/fr/annonce/4712345-identical-title"),
            replace(_VARIANT, direct_url="https://jobs.implid.com/fr/annonce/4712346-identical-title"),
            [1, 1],
            id="different-employer-ids-with-identical-copy",
        ),
        pytest.param(
            replace(_BASE, direct_url="https://careers.dcsit-group.com/fr/annonce/712345-description"),
            replace(_VARIANT, direct_url="https://careers.dcsit-group.com/fr/annonce/712345"),
            [2],
            id="unadorned-announcement-url",
        ),
        pytest.param(
            replace(_BASE, direct_url="https://recrutement.alma.fr/poste/ab12cd34ef-description/"),
            replace(_VARIANT, direct_url="https://recrutement.alma.fr/poste/ab12cd34ef"),
            [2],
            id="stable-alphanumeric-posting-id",
        ),
        pytest.param(
            replace(_BASE, direct_url="https://example.talent-soft.com/offre-de-emploi/emploi-com_76543.aspx"),
            replace(_VARIANT, direct_url="https://example.talent-soft.com/offre-de-emploi/communication_76543.aspx"),
            [2],
            id="talentsoft-posting-id-after-slug",
        ),
        pytest.param(
            _BASE,
            replace(
                _BASE,
                source_url="https://fr.indeed.com/viewjob?jk=synthetic-right",
                title="Responsable de communication du service culturel",
                description="Vous pilotez la stratégie de communication et le service des relations publiques.",
            ),
            [1, 1],
            id="same-board-title-with-different-duties",
        ),
    ],
)
def test_synthetic_posting_pairs(first: JobOffer, second: JobOffer, expected: list[int], tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        for offer in (first, second):
            store.upsert_offer(offer)
        canonical = store.canonical_dataframe()
        assert sorted(canonical["duplicate_count"].tolist()) == expected
        assert len(store.raw_dataframe()) == 2
        assert {url for links in canonical["all_source_links"] for url in links.splitlines()} == {
            first.source_url,
            second.source_url,
        }
    finally:
        store.close()


@given(st.integers(min_value=1, max_value=999_999_999), st.permutations((0, 1, 2, 3)))
def test_distinct_employer_requisitions_stay_separate_in_any_input_order(posting_id: int, order: list[int]) -> None:
    rows = [
        {
            "source": "indeed" if index % 2 else "linkedin",
            "source_url": f"https://board.example/jobs/{index}",
            "source_id": str(index),
            "direct_url": f"https://jobs.implid.com/fr/annonce/{posting_id + index // 2}-variant-{index}",
            "title": _BASE.title,
            "company": _BASE.company,
            "location": "Ville-Exemple 2e" if index % 2 else "Ville-Exemple",
            "description": _DESCRIPTION,
            "description_key": None,
            "dedupe_key": None,
        }
        for index in order
    ]
    keys = build_group_keys(
        pd.DataFrame(rows), ("title", "company"), ("location",), ("description", "company", "location")
    )
    group_to_id = dict(zip(keys, (index // 2 for index in order), strict=True))
    assert len(group_to_id) == 2
    assert [group_to_id[key] for key in keys] == [index // 2 for index in order]


def test_export_merges_historical_posting_urls_and_retains_state(tmp_path: Path) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    (output / "jobs.csv").write_text(
        "Statut,Lien,Intitulé,À vérifier\n"
        "En Attente,https://jobs.implid.com/fr/annonce/4712345-original,Communication,\n"
        "Postulé,https://jobs.implid.com/fr/annonce/4712345-revised,Communication,\n"
        "Refusé,https://jobs.implid.com/fr/annonce/4712346-distinct,Communication,\n",
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "empty.duckdb")
    try:
        for _ in range(2):
            (path,), total = export_job_formats(
                store, output, ("Statut", "Lien", "Intitulé", "À vérifier"), formats=("xlsx",)
            )
            assert total == 2
            book = load_workbook(path, read_only=True)
            try:
                first, second = list(book["Offres"].values)[1:]
                assert first[0] == "Postulé"
                assert first[3] == "Statuts divergents entre doublons : En Attente / Postulé"
                assert second[0] == "Refusé"
            finally:
                book.close()
    finally:
        store.close()


def test_purged_internship_stays_excluded_after_posting_slug_changes(tmp_path: Path) -> None:
    output = tmp_path / "exports"
    output.mkdir()
    (output / "jobs.csv").write_text(
        "Statut,Lien,Intitulé\nPostulé,https://jobs.implid.com/fr/annonce/4712345-new-slug,Communication\n",
        encoding="utf-8",
    )
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            replace(
                _BASE,
                direct_url="https://jobs.implid.com/fr/annonce/4712345-old-slug",
                employment_type="Stage",
            )
        )
        assert store.purge_excluded_offers() == 1
        assert export_job_formats(store, output, ("Statut", "Lien", "Intitulé"), formats=("xlsx",))[1] == 0
    finally:
        store.close()
