from __future__ import annotations

import os
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
import pytest
from hypothesis import HealthCheck, settings

if TYPE_CHECKING:
    from scrapper.config.settings import LocationScopeConfig

os.environ.setdefault("SCRAPPER_DISABLE_LOCAL_CONFIG", "1")

settings.register_profile("local", max_examples=50)
settings.register_profile("ci", max_examples=500)
settings.register_profile("fast", max_examples=10, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.getenv("PROFILE", "local"))


@pytest.fixture
def toulouse_scope() -> LocationScopeConfig:
    from scrapper.config.settings import LocationCenterConfig, LocationScopeConfig

    return LocationScopeConfig(
        radius_km=25,
        centers=(LocationCenterConfig(name="Toulouse", latitude=43.6007, longitude=1.4328),),
        city_coordinates={
            "Blagnac": (43.6410, 1.3770),
            "Léguevin": (43.5928, 1.2353),
            "Ramonville-Saint-Agne": (43.5438, 1.4784),
            "Saint-Orens-de-Gameville": (43.5550, 1.5332),
            "Montauban": (44.0198, 1.3550),
        },
    )


@pytest.fixture
def mock_html_client() -> Callable[[str], httpx.AsyncClient]:
    def make_client(html: str) -> httpx.AsyncClient:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html; charset=UTF-8"})
        )
        return httpx.AsyncClient(transport=transport, follow_redirects=True)

    return make_client


@pytest.fixture
def jobijoba_alternance_page() -> tuple[str, str]:
    return (
        "https://www.jobijoba.com/fr/annonce/54/66cdbc5f83c4667724e965945b83f122",
        """
    <html>
      <head>
        <meta name="description" content="Voir l'offre d'emploi - Jobijoba">
      </head>
      <body>
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
      </body>
    </html>
    """,
    )


@pytest.fixture
def emploi_territorial_page() -> tuple[str, str]:
    return (
        "https://www.emploi-territorial.fr/offre/o031-example-charge-communication",
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
    )
