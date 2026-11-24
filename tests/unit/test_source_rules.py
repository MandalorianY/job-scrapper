from __future__ import annotations

from bs4 import BeautifulSoup

from jobsfr.source_rules import get_source_rules
from jobsfr.source_rules.common import normalize_offer_title
from jobsfr.source_rules.france_travail import infer_company_from_description


def test_common_title_normalizer_removes_offer_reference_prefixes() -> None:
    assert normalize_offer_title("Offre n° 208WKXK Chargé de communication (F/H)") == "Chargé de communication (F/H)"
    assert (
        normalize_offer_title("208JPTS Offre d'emploi - Chargé(e) de Communication H/F (H/F)")
        == "Chargé(e) de Communication H/F (H/F)"
    )


def test_france_travail_infers_company_from_description_only_when_explicit() -> None:
    assert (
        infer_company_from_description("Lead and Connect France est une société spécialisée dans la relation client.")
        == "Lead and Connect France"
    )
    assert (
        infer_company_from_description(
            "Votre objectif principal sera de mettre en œuvre la stratégie de communication."
        )
        is None
    )


def test_france_travail_page_extractors_read_employer_location_and_date() -> None:
    soup = BeautifulSoup(
        """
        <div class="modal-body">
          <span itemprop="datePosted" content="2026-05-28">Publié le 28 mai 2026</span>
          <p class="title-complementary">31 - Blagnac - Localiser avec Mappy</p>
          <h2>Employeur</h2>
          <div class="media">
            <div class="media-body">
              <h3 class="title">RANDSTAD</h3>
              <a href="/page-employeur/randstad-093">Voir la page employeur</a>
            </div>
          </div>
        </div>
        """,
        "lxml",
    )
    rules = get_source_rules("france_travail")
    url = "https://candidat.francetravail.fr/offres/recherche/detail/208WKXK"

    assert rules.extract_company(soup, url) == "RANDSTAD"
    assert rules.extract_company_url(soup, url) == "https://candidat.francetravail.fr/page-employeur/randstad-093"
    assert rules.extract_location(soup, url) == "Blagnac"
    assert rules.extract_date(soup, url) == "2026-05-28"


def test_jobijoba_extracts_current_offer_block_instead_of_navigation_text(
    jobijoba_alternance_page: tuple[str, str],
) -> None:
    url, html = jobijoba_alternance_page
    soup = BeautifulSoup(html, "lxml")
    rules = get_source_rules("jobijoba")

    assert rules.extract_location(soup, url) == "Castelginest"
    assert rules.extract_employment_type(soup, url) == "Alternance"
    assert rules.extract_date(soup, url) == "Il y a 10 h"
    assert rules.extract_salary(soup, url) == "De 780 € à 1 222 € par mois"
    assert "alternant(e)" in (rules.extract_description(soup, url) or "")


def test_jobijoba_extracts_visible_permalink_date_without_trailing_content() -> None:
    soup = BeautifulSoup(
        """
        <div class="offer current">
          <div class="permalink-content">
            <div class="permalink-date">Publiée le 4 juin</div>
            <div class="permalink-subtitle">Description de l'offre</div>
            <p class="permalink-description">Poste situé à Quint-Fonsegrives.</p>
          </div>
        </div>
        """,
        "lxml",
    )
    rules = get_source_rules("jobijoba")
    url = "https://www.jobijoba.com/fr/annonce/54/2daeed2968112308d7cd8a5f4c119f28"

    assert rules.extract_date(soup, url) == "4 juin"


def test_jobijoba_date_parser_does_not_glue_description_text_to_relative_dates() -> None:
    soup = BeautifulSoup(
        """
        <div class="offer current">
          <span>Publiée le Il y a 10 h Description de l'offre</span>
        </div>
        """,
        "lxml",
    )
    rules = get_source_rules("jobijoba")
    url = "https://www.jobijoba.com/fr/annonce/54/2daeed2968112308d7cd8a5f4c119f28"

    assert rules.extract_date(soup, url) == "Il y a 10 h"


def test_emploi_territorial_extracts_labeled_offer_fields(emploi_territorial_page: tuple[str, str]) -> None:
    url, html = emploi_territorial_page
    soup = BeautifulSoup(html, "lxml")
    rules = get_source_rules("emploi_territorial")

    assert rules.extract_title(soup, url) == "Chargé de communication"
    assert rules.extract_company(soup, url) == "MAIRIE DE TOULOUSE"
    assert rules.extract_company_url(soup, url) == "https://www.toulouse.fr"
    assert rules.extract_location(soup, url) == "Toulouse (Haute-Garonne (31))"
    assert rules.extract_employment_type(soup, url) == "Emploi permanent - vacance d'emploi"
    assert rules.extract_date(soup, url) == "04/06/2026"
    assert "Communication institutionnelle" in (rules.extract_description(soup, url) or "")
