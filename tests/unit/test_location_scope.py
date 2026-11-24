from __future__ import annotations

import pytest

from jobsfr.config.settings import LocationScopeConfig
from jobsfr.geography.scope import location_is_allowed, location_is_in_scope
from jobsfr.source_rules import get_source_rules


@pytest.mark.parametrize(
    "location",
    [
        "Toulouse, Occitanie, France",
        "Blagnac, Occitanie, FR",
        "Ramonville-Saint-Agne, Occitanie, FR",
        "Leguevin, Occitanie, FR",
        "31 - Toulouse",
        "31 - St Orens de Gameville",
        "4 rue du Périgord, Toulouse (Haute-Garonne (31))",
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
        "Toulouze, Occitanie, France",
        "4 rue de Toulouse, Montauban (82)",
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


@pytest.mark.parametrize(
    ("town", "label"),
    [("Toulouse", "Ville de Toulouse"), ("Blagnac", "Blagnacc")],
)
def test_unrecognized_municipal_labels_follow_the_configured_policy(
    town: str, label: str, toulouse_scope: LocationScopeConfig
) -> None:
    review_scope = LocationScopeConfig(centers=toulouse_scope.centers, city_coordinates=toulouse_scope.city_coordinates)
    assert location_is_in_scope(town, review_scope)
    assert location_is_allowed(label, review_scope)
    assert not location_is_in_scope(label, review_scope)
    assert not location_is_allowed(label, toulouse_scope)


def test_emploi_territorial_postal_box_prefix_keeps_the_work_town(toulouse_scope: LocationScopeConfig) -> None:
    normalize = get_source_rules("emploi_territorial").normalize_location
    assert location_is_in_scope(normalize("BP295, Toulouse (Haute-Garonne (31))"), toulouse_scope)
    assert not location_is_in_scope(normalize("B.P. 6, Montauban (82)"), toulouse_scope)
