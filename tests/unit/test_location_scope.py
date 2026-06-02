from __future__ import annotations

import pytest

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope


@pytest.mark.parametrize(
    "location",
    [
        "Toulouse, Occitanie, France",
        "Blagnac, Occitanie, FR",
        "Blagnac, Occitanie, FR",
        "Grand Toulouse",
        "31 - Toulouse 6e Arrondissement",
        "CHARGÉ DE RELATIONS PRESSE - UNIVERS MONTAGNE (H/F) 31 - Blagnac",
    ],
)
def test_location_scope_accepts_towns_within_configured_radius(location: str) -> None:
    assert location_is_in_scope(location, SearchConfig().location_scope)


@pytest.mark.parametrize(
    "location",
    [
        "Thiers, Auvergne-Rhône-Alpes, France",
        "Clermont-Ferrand, Puy-de-Dôme, Auvergne-Rhone-Alpes, France",
        "Paris, Paris, Ile-de-France, France",
        "Rhône-Alpes",
        None,
    ],
)
def test_location_scope_rejects_out_of_radius_or_broad_locations(location: str | None) -> None:
    assert not location_is_in_scope(location, SearchConfig().location_scope)
