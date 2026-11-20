from __future__ import annotations

from bs4 import BeautifulSoup

from scrapper.source_rules import get_source_rules
from scrapper.source_rules.common import normalize_offer_title
from scrapper.source_rules.france_travail import extract_mappy_location, infer_company_from_description


def test_common_title_normalizer_removes_offer_reference_prefixes() -> None:
    assert normalize_offer_title("Offre n° 208WKXK Chargé de communication (F/H)") == "Chargé de communication (F/H)"
    assert normalize_offer_title("208JPTS Offre d'emploi - Chargé(e) de Communication H/F (H/F)") == "Chargé(e) de Communication H/F (H/F)"


def test_france_travail_location_parser_returns_city_name_only() -> None:
    assert extract_mappy_location("31 - Blagnac - Localiser avec Mappy") == "Blagnac"


def test_france_travail_infers_company_from_description_only_when_explicit() -> None:
    assert infer_company_from_description("Lead and Connect France est une société spécialisée dans la relation client.") == "Lead and Connect France"
    assert infer_company_from_description("Votre objectif principal sera de mettre en œuvre la stratégie de communication.") is None


def test_france_travail_repairs_tracker_fields() -> None:
    rules = get_source_rules("france_travail")
    assert rules.repair_tracker_fields(
        "208WKXK Chargé de communication (F/H)",
        "31 - Blagnac - Localiser avec Mappy",
        "Toulouse",
        "Votre objectif principal sera de mettre en œuvre la stratégie de communication.",
    ) == ("Chargé de communication (F/H)", None, "Blagnac")


def test_france_travail_page_extractors_live_in_dedicated_rules_module() -> None:
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


def test_jobijoba_extracts_visible_permalink_date_without_trailing_content() -> None:
    soup = BeautifulSoup(
        """
        <div class="offer current">
          <div class="permalink-content">
            <div class="permalink-date">Publiée le 4 juin</div>
            <div class="permalink-subtitle">Description de l'offre</div>
            <p class="permalink-description">Poste situé à Charbonnières-les-Bains.</p>
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


def test_emploi_territorial_extracts_labeled_offer_fields() -> None:
    soup = BeautifulSoup(
        """
        <html>
          <head>
            <title>Chargé de communication - MAIRIE DE TOULOUSE | emploi-territorial.fr</title>
            <meta property="og:title" content="www.emploi-territorial.fr">
          </head>
          <body>
            <main>
              <h1 class="set-header-logo-h1"></h1>
              <h2 class="set-line-emploi">Chargé de communication</h2>
              <div class="offre-item row p-2">
                <div class="offre-item-label col-4 font-weight-bold">Employeur</div>
                <div class="offre-item-value col-8"><strong>MAIRIE DE TOULOUSE</strong></div>
              </div>
              <div class="offre-item row p-2">
                <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Site web de l'employeur</div>
                <div class="offre-item-value col-12 col-sm-8">
                  <a href="https://www.toulouse.fr">https://www.toulouse.fr</a>
                </div>
              </div>
              <div class="offre-item row p-2">
                <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Lieu de travail</div>
                <div class="offre-item-value col-12 col-sm-8">Toulouse (Haute-Garonne (31))</div>
              </div>
              <div class="offre-item row p-2">
                <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Type d'emploi</div>
                <div class="offre-item-value col-12 col-sm-8">Emploi permanent - vacance d'emploi</div>
              </div>
              <div class="offre-item row p-2">
                <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Publiée le</div>
                <div class="offre-item-value col-12 col-sm-8">04/06/2026</div>
              </div>
              <div class="offre-item row p-2 font-weight-bold">
                <div class="offre-item-label col">Descriptif de l'emploi</div>
              </div>
              <div class="offre-item-text row px-3 mb-3">
                Communication institutionnelle et relations presse.
              </div>
              <div class="offre-item row p-2 font-weight-bold">
                <div class="offre-item-label col">Missions / conditions d'exercice</div>
              </div>
              <div class="offre-item-text row px-3 mb-3">
                Gérer les contenus web et les supports de communication.
              </div>
            </main>
          </body>
        </html>
        """,
        "lxml",
    )
    rules = get_source_rules("emploi_territorial")
    url = "https://www.emploi-territorial.fr/offre/o069-example-charge-communication"

    assert rules.extract_title(soup, url) == "Chargé de communication"
    assert rules.extract_company(soup, url) == "MAIRIE DE TOULOUSE"
    assert rules.extract_company_url(soup, url) == "https://www.toulouse.fr"
    assert rules.extract_location(soup, url) == "Toulouse (Haute-Garonne (31))"
    assert rules.extract_employment_type(soup, url) == "Emploi permanent - vacance d'emploi"
    assert rules.extract_date(soup, url) == "04/06/2026"
    assert "Communication institutionnelle" in (rules.extract_description(soup, url) or "")
