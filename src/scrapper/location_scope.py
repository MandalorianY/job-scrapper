from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from functools import lru_cache
from typing import Protocol


class LocationCenter(Protocol):
    name: str
    latitude: float
    longitude: float


class LocationScope(Protocol):
    enabled: bool
    radius_km: float
    city_coordinates: dict[str, tuple[float, float]]

    @property
    def centers(self) -> tuple[LocationCenter, ...]: ...


def location_is_in_scope(location: object | None, scope: LocationScope) -> bool:
    if not scope.enabled:
        return True
    text = _normalize(location)
    if not text:
        return False

    pattern = _scope_pattern(
        scope.radius_km,
        tuple((center.name, center.latitude, center.longitude) for center in scope.centers),
        tuple(scope.city_coordinates.items()),
    )
    return pattern is not None and pattern.search(text) is not None


def coordinates_are_in_scope(location: tuple[float, float], scope: LocationScope) -> bool:
    return _within_radius(location, scope.radius_km, ((center.latitude, center.longitude) for center in scope.centers))


def _within_radius(location: tuple[float, float], radius_km: float, centers: Iterable[tuple[float, float]]) -> bool:
    return any(_distance_km(location, center) <= radius_km for center in centers)


@lru_cache(maxsize=32)
def _scope_pattern(
    radius_km: float,
    centers: tuple[tuple[str, float, float], ...],
    cities: tuple[tuple[str, tuple[float, float]], ...],
) -> re.Pattern[str] | None:
    coordinates = {normalized: point for city, point in cities if (normalized := _normalize(city))}
    coordinates.update({_normalize(city): (latitude, longitude) for city, latitude, longitude in centers})
    center_points = tuple((latitude, longitude) for _, latitude, longitude in centers)
    names = [
        re.escape(city)
        for city, point in coordinates.items()
        if city and _within_radius(point, radius_km, center_points)
    ]
    return re.compile(r"\b(?:" + "|".join(names) + r")\b") if names else None


def _distance_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    radius_km = 6371.0088
    left_lat, left_lon = math.radians(left[0]), math.radians(left[1])
    right_lat, right_lon = math.radians(right[0]), math.radians(right[1])
    delta_lat = right_lat - left_lat
    delta_lon = right_lon - left_lon
    haversine = math.sin(delta_lat / 2) ** 2 + math.cos(left_lat) * math.cos(right_lat) * math.sin(delta_lon / 2) ** 2
    return 2 * radius_km * math.asin(math.sqrt(haversine))


def _normalize(value: object | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).casefold()
    return re.sub(r"\s+", " ", text).strip()
