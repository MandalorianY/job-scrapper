from __future__ import annotations

import pytest

from scrapper.config.settings import LocationScopeConfig
from scrapper.location_scope import location_is_in_scope


@pytest.mark.parametrize(
    "location",
    [
        "Toulouse, Occitanie, France",
        "Blagnac, Occitanie, FR",
        "Ramonville-Saint-Agne, Occitanie, FR",
        "Leguevin, Occitanie, FR",
        "31 - Toulouse",
        "CHARGÉ DE RELATIONS PRESSE - UNIVERS MONTAGNE (H/F) 31 - Saint-Orens-de-Gameville",
    ],
)
def test_location_scope_accepts_towns_within_configured_radius(
    location: str, toulouse_scope: LocationScopeConfig
) -> None:
    assert location_is_in_scope(location, toulouse_scope)


@pytest.mark.parametrize(
    "location",
    [
        "Montauban, Occitanie, France",
        "Lille, Nord, Hauts-de-France, France",
        "Paris, Paris, Ile-de-France, France",
        "Occitanie",
        None,
    ],
)
def test_location_scope_rejects_out_of_radius_or_broad_locations(
    location: str | None, toulouse_scope: LocationScopeConfig
) -> None:
    assert not location_is_in_scope(location, toulouse_scope)
