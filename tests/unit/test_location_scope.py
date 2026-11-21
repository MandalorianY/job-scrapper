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


def test_location_scope_checks_complete_towns_and_explicit_regions(toulouse_scope: LocationScopeConfig) -> None:
    scope = toulouse_scope.model_copy(
        update={"region_names": ("Occitanie",), "city_coordinates": {"Saint-Jean": (43.661, 1.506)}}
    )
    assert location_is_in_scope("Toulouse, Occitanie, France", scope)
    assert location_is_in_scope("Greater Toulouse Area", scope)
    assert location_is_in_scope("31000 Toulouse", scope)
    assert location_is_in_scope("Paris, Île-de-France, France / Toulouse, Occitanie, France", scope)
    assert not location_is_in_scope("Toulouse, Île-de-France, France", scope)
    assert not location_is_in_scope("Saint-Jean-de-Luz", scope)


def test_location_scope_accepts_nearby_towns_and_abbreviations(
    toulouse_scope: LocationScopeConfig,
) -> None:
    scope = toulouse_scope.model_copy(
        update={"city_coordinates": {"Portet-sur-Garonne": (43.60, 1.43), "Saint-Jean": (43.59, 1.43)}}
    )
    assert location_is_in_scope("Portet-sur-Garonne", scope)
    assert location_is_in_scope("31 - St Jean", scope)
    assert not location_is_in_scope("Saint-Jean-de-Luz", scope)
