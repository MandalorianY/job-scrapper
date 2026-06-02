from __future__ import annotations

from bs4 import BeautifulSoup

from scrapper.source_rules import get_source_rules
from scrapper.source_rules.common import normalize_offer_title
from scrapper.source_rules.france_travail import extract_mappy_location, infer_company_from_description


def test_common_title_normalizer_removes_offer_reference_prefixes() -> None:
    assert normalize_offer_title("Offre n° 208WKXK Chargé de communication (F/H)") == "Chargé de communication (F/H)"
    assert normalize_offer_title("208JPTS Offre d'emploi - Chargé(e) de Communication H/F (H/F)") == "Chargé(e) de Communication H/F (H/F)"


def test_france_travail_location_parser_returns_city_name_only() -> None:
    assert extract_mappy_location("69 - Pusignan - Localiser avec Mappy") == "Pusignan"


def test_france_travail_infers_company_from_description_only_when_explicit() -> None:
    assert infer_company_from_description("Lead and Connect France est une société spécialisée dans la relation client.") == "Lead and Connect France"
    assert infer_company_from_description("Votre objectif principal sera de mettre en œuvre la stratégie de communication.") is None


def test_france_travail_repairs_tracker_fields() -> None:
    rules = get_source_rules("france_travail")
    assert rules.repair_tracker_fields(
        "208WKXK Chargé de communication (F/H)",
        "69 - Pusignan - Localiser avec Mappy",
        "Lyon",
        "Votre objectif principal sera de mettre en œuvre la stratégie de communication.",
    ) == ("Chargé de communication (F/H)", None, "Pusignan")


def test_france_travail_page_extractors_live_in_dedicated_rules_module() -> None:
    soup = BeautifulSoup(
        """
        <div class="modal-body">
          <p class="title-complementary">69 - Pusignan - Localiser avec Mappy</p>
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
    assert rules.extract_location(soup, url) == "Pusignan"


def test_jobijoba_extracts_current_offer_block_instead_of_navigation_text() -> None:
    soup = BeautifulSoup(
        """
        <nav>
          <span>Emploi CDI/CDD</span>
          <span>Offres d'alternance</span>
        </nav>
        <div class="offer current">
          <div class="row permalink-open">
            <h1 class="permalink-title">Chargé de communication et marketing digital h/f</h1>
            <span>Annemasse</span>
            <span>Alternance</span>
            <span>Chargé de communication</span>
            <span>De 780 € à 1 222 € par mois</span>
          </div>
          <span>Publiée le Il y a 10 h</span>
          <h2>Description de l'offre</h2>
          <p class="permalink-description">
            Dans le cadre de son partenariat avec une entreprise spécialisée dans l'immobilier,
            notre école recherche un(e) alternant(e) pour occuper un poste.
          </p>
        </div>
        """,
        "lxml",
    )
    rules = get_source_rules("jobijoba")
    url = "https://www.jobijoba.com/fr/annonce/54/66cdbc5f83c4667724e965945b83f122"

    assert rules.extract_location(soup, url) == "Annemasse"
    assert rules.extract_employment_type(soup, url) == "Alternance"
    assert rules.extract_date(soup, url) == "Il y a 10 h"
    assert rules.extract_salary(soup, url) == "De 780 € à 1 222 € par mois"
    assert "alternant(e)" in (rules.extract_description(soup, url) or "")
