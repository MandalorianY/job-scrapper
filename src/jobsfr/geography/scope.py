from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Literal, Protocol

FRENCH_REGIONS = frozenset(
    {
        "auvergne rhone alpes",
        "bourgogne franche comte",
        "bretagne",
        "centre val de loire",
        "corse",
        "grand est",
        "hauts de france",
        "ile de france",
        "normandie",
        "nouvelle aquitaine",
        "occitanie",
        "pays de la loire",
        "provence alpes cote d azur",
        "guadeloupe",
        "guyane",
        "la reunion",
        "martinique",
        "mayotte",
    }
)
REGION_ALIASES = {"ara": "auvergne rhone alpes", "rhone alpes": "auvergne rhone alpes"}
STREET_ADDRESS_RE = re.compile(
    r"^\s*\d{1,4}(?:\s*(?:bis|ter))?\s+(?:rue|avenue|boulevard|all[eé]e|chemin|impasse|passage|place|quai|route)\b",
    re.IGNORECASE,
)


class LocationCenter(Protocol):
    name: str
    latitude: float
    longitude: float


class LocationScope(Protocol):
    enabled: bool
    include_unrecognized: bool
    radius_km: float
    city_coordinates: dict[str, tuple[float, float]]
    region_names: tuple[str, ...]
    known_city_names: frozenset[str]

    @property
    def centers(self) -> tuple[LocationCenter, ...]: ...


def location_is_in_scope(location: object | None, scope: LocationScope) -> bool:
    return _location_match(location, scope) == "inside"


def location_is_allowed(location: object | None, scope: LocationScope) -> bool:
    match = _location_match(location, scope)
    return match == "inside" or (scope.include_unrecognized and match == "unrecognized")


def location_needs_review(location: object | None, scope: LocationScope) -> bool:
    return scope.include_unrecognized and _location_match(location, scope) == "unrecognized"


def _location_match(location: object | None, scope: LocationScope) -> Literal["inside", "outside", "unrecognized"]:
    if not scope.enabled:
        return "inside"
    if not location:
        return "outside"
    address = re.sub(r"^.*\)\s+(?=\d{2,5}\s*[-–])", "", str(location))
    pattern, known_coordinates = _patterns_for_scope(scope)
    if pattern is None:
        return "outside"
    expected = {normalize_location_text(region) for region in scope.region_names}
    unrecognized = False
    for place in address.split("/"):
        locality, *qualifiers = re.split(r"[,()]", place)
        if qualifiers and STREET_ADDRESS_RE.match(locality):
            locality, *qualifiers = qualifiers
        regions = {REGION_ALIASES.get(name, name) for part in qualifiers if (name := normalize_location_text(part))}
        if expected and (regions & FRENCH_REGIONS) - expected:
            continue
        locality = re.split(r"\b\d{2,5}\s*[-–]\s*", locality)[-1]
        name = normalize_location_text(locality)
        name = re.sub(
            r"^(?:greater |\d{2,5} )"
            r"| (?:area|et peripherie|\d{2,5}|\d{1,2}(?:er|e|eme)?(?: arrondissement)?)$",
            "",
            name,
        )
        if pattern.fullmatch(name):
            return "inside"
        if (
            name
            and not name.isdecimal()
            and name not in {"fr", "france", "remote", "teletravail"}
            and REGION_ALIASES.get(name, name) not in FRENCH_REGIONS
            and name not in known_coordinates
            and name not in scope.known_city_names
        ):
            unrecognized = True
    return "unrecognized" if unrecognized else "outside"


def mentions_location_in_scope(text: object | None, scope: LocationScope) -> bool:
    if not scope.enabled:
        return True
    pattern, _ = _patterns_for_scope(scope)
    return pattern is not None and pattern.search(normalize_location_text(text)) is not None


def _patterns_for_scope(scope: LocationScope) -> tuple[re.Pattern[str] | None, frozenset[str]]:
    return _scope_patterns(
        scope.radius_km,
        tuple((center.name, center.latitude, center.longitude) for center in scope.centers),
        tuple(scope.city_coordinates.items()),
    )


def coordinates_are_in_scope(location: tuple[float, float], scope: LocationScope) -> bool:
    return _within_radius(location, scope.radius_km, ((center.latitude, center.longitude) for center in scope.centers))


def _within_radius(location: tuple[float, float], radius_km: float, centers: Iterable[tuple[float, float]]) -> bool:
    return any(_distance_km(location, center) <= radius_km for center in centers)


@lru_cache(maxsize=32)
def _scope_patterns(
    radius_km: float,
    centers: tuple[tuple[str, float, float], ...],
    cities: tuple[tuple[str, tuple[float, float]], ...],
) -> tuple[re.Pattern[str] | None, frozenset[str]]:
    coordinates = {normalized: point for city, point in cities if (normalized := normalize_location_text(city))}
    coordinates.update({normalize_location_text(city): (latitude, longitude) for city, latitude, longitude in centers})
    center_points = tuple((latitude, longitude) for _, latitude, longitude in centers)
    nearby = {city for city, point in coordinates.items() if city and _within_radius(point, radius_km, center_points)}
    pattern = re.compile(r"\b(?:" + "|".join(map(re.escape, sorted(nearby))) + r")\b") if nearby else None
    return pattern, frozenset(coordinates)


def _distance_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    radius_km = 6371.0088
    left_lat, left_lon = math.radians(left[0]), math.radians(left[1])
    right_lat, right_lon = math.radians(right[0]), math.radians(right[1])
    delta_lat = right_lat - left_lat
    delta_lon = right_lon - left_lon
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(left_lat) * math.cos(right_lat) * math.sin(delta_lon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(haversine))


def normalize_location_text(value: object | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).casefold()
    text = re.sub(r"\bste\s+", "sainte ", text)
    text = re.sub(r"\bst\s+", "saint ", text)
    return re.sub(r"\s+", " ", text).strip()
