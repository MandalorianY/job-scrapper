from __future__ import annotations

import math
import re
import unicodedata
from typing import Protocol


class LocationCenter(Protocol):
    name: str
    latitude: float
    longitude: float
    aliases: tuple[str, ...]


class LocationScope(Protocol):
    enabled: bool
    radius_km: float
    centers: tuple[LocationCenter, ...]
    city_coordinates: dict[str, tuple[float, float]]


def location_is_in_scope(location: object | None, scope: LocationScope) -> bool:
    if not scope.enabled:
        return True
    text = _normalize(location)
    if not text:
        return False

    coordinates = _coordinate_lookup(scope)
    matched = _matched_coordinates(text, coordinates)
    return any(
        _distance_km(city_coordinates, (center.latitude, center.longitude)) <= scope.radius_km
        for city_coordinates in matched
        for center in scope.centers
    )


def _matched_coordinates(text: str, coordinates: dict[str, tuple[float, float]]) -> list[tuple[float, float]]:
    matches: list[tuple[float, float]] = []
    for key in sorted(coordinates, key=len, reverse=True):
        if re.search(rf"(?<![a-z0-9]){re.escape(key)}(?![a-z0-9])", text):
            matches.append(coordinates[key])
    return matches


def _coordinate_lookup(scope: LocationScope) -> dict[str, tuple[float, float]]:
    coordinates = {
        _normalize(city): (float(lat_lng[0]), float(lat_lng[1]))
        for city, lat_lng in scope.city_coordinates.items()
        if _normalize(city)
    }
    for center in scope.centers:
        center_coordinates = (float(center.latitude), float(center.longitude))
        for value in (center.name, *center.aliases):
            normalized = _normalize(value)
            if normalized:
                coordinates[normalized] = center_coordinates
    return coordinates


def _distance_km(left: tuple[float, float], right: tuple[float, float]) -> float:
    radius_km = 6371.0088
    left_lat, left_lon = math.radians(left[0]), math.radians(left[1])
    right_lat, right_lon = math.radians(right[0]), math.radians(right[1])
    delta_lat = right_lat - left_lat
    delta_lon = right_lon - left_lon
    haversine = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(left_lat) * math.cos(right_lat) * math.sin(delta_lon / 2) ** 2
    )
    return 2 * radius_km * math.asin(math.sqrt(haversine))


def _normalize(value: object | None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFKD", str(value))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = re.sub(r"[^a-zA-Z0-9]+", " ", text).casefold()
    return re.sub(r"\s+", " ", text).strip()
