import json
import re
from collections.abc import Callable
from contextlib import nullcontext
from functools import partial
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

import httpx
import structlog
from pydantic import BaseModel

from scrapper.config import SearchConfig
from scrapper.config.settings import LocationCenterConfig, LocationScopeConfig, SiteLocationConfig
from scrapper.location_scope import coordinates_are_in_scope, normalize_location_text
from scrapper.text import normalize_text

GEO_API = "https://geo.api.gouv.fr"
log = structlog.get_logger(__name__)


class _Municipality(BaseModel):
    center: LocationCenterConfig
    department: str
    region_code: str


class _Region(BaseModel):
    name: str
    municipalities: tuple[LocationCenterConfig, ...]


def route_location_overrides(
    config: SearchConfig, *, write_cache: bool = True, cache_only: bool = False
) -> SearchConfig:
    if config.country.casefold() != "france" or not config.search_locations:
        return config
    cache_dir = config.database_path.parent / ".geography"
    municipalities = {}
    connection = nullcontext() if cache_only else httpx.Client(base_url=GEO_API, timeout=10)
    with connection as client:
        for location in config.search_locations:
            city = location.split(",", 1)[0].strip()
            town = _cached_geography(
                cache_dir / f"town-{normalize_text(city)}.json",
                _Municipality,
                partial(_fetch_municipality, client, city) if client is not None else None,
                write_cache=write_cache,
            )
            if town is None:
                return config
            municipalities[location] = town
        regions = {}
        for code in dict.fromkeys(town.region_code for town in municipalities.values()):
            region = _cached_geography(
                cache_dir / f"region-{normalize_text(code)}.json",
                _Region,
                partial(_fetch_region, client, code) if client is not None else None,
                write_cache=write_cache,
            )
            if region is None:
                return config
            regions[code] = region

    centers = tuple(town.center for town in municipalities.values())
    cities = [center.name for center in centers]
    scope = config.location_scope.model_copy(update={"centers": centers})
    coordinates = {}
    for region in regions.values():
        for town in region.municipalities:
            point = (town.latitude, town.longitude)
            # Several municipalities can share a name, even within a region.
            if coordinates_are_in_scope(point, scope):
                coordinates.setdefault(town.name, point)

    site_locations = {
        location: config.aggregators.site_locations.get(
            location,
            SiteLocationConfig(
                department=town.department,
                region_slug=normalize_text(regions[town.region_code].name).replace(" ", "-"),
            ),
        )
        for location, town in municipalities.items()
    }

    if scope.city_aliases:
        points = {normalize_location_text(city): point for city, point in coordinates.items()}
        points.update({normalize_location_text(center.name): (center.latitude, center.longitude) for center in centers})
        for city, aliases in scope.city_aliases.items():
            if (point := points.get(normalize_location_text(city))) is not None:
                coordinates.update(dict.fromkeys(aliases, point))
    nearby_names = {city.casefold() for city in coordinates}
    nearby_names.update(center.name.casefold() for center in centers)
    regional_names = tuple(sorted(nearby_names, key=lambda city: (-len(city), city)))
    scope = scope.model_copy(
        update={
            "city_coordinates": coordinates,
            "region_names": tuple(region.name for region in regions.values()),
            "known_city_names": _known_city_names(cache_dir, regions, scope.city_aliases)
            if scope.include_unrecognized
            else frozenset(),
        }
    )
    return config.model_copy(
        update={
            "location_scope": scope,
            "aggregators": config.aggregators.model_copy(
                update={
                    "location_terms": tuple(dict.fromkeys((*regional_names, *config.aggregators.location_terms))),
                    "direct_locations": config.aggregators.direct_locations or tuple(f"{code}R" for code in regions),
                    "site_locations": site_locations,
                    "direct_specs": tuple(
                        spec.model_copy(
                            update={
                                "search_url_templates": _direct_templates(
                                    spec.source,
                                    spec.search_url_templates,
                                    cities,
                                    tuple(regions),
                                )
                            }
                        )
                        for spec in config.aggregators.direct_specs
                    ),
                }
            ),
        }
    )


def _known_city_names(
    cache_dir: Path, regions: dict[str, _Region], aliases: dict[str, tuple[str, ...]]
) -> frozenset[str]:
    known_regions = list(regions.values())
    for path in cache_dir.glob("region-*.json"):
        if path.stem.removeprefix("region-") not in regions and (region := _load_geography(path, _Region, warn=False)):
            known_regions.append(region)
    names = {normalize_location_text(town.name) for region in known_regions for town in region.municipalities}
    for city, spellings in aliases.items():
        if normalize_location_text(city) in names:
            names.update(normalize_location_text(spelling) for spelling in spellings)
    return frozenset(names)


def load_location_scope(config: SearchConfig, database: Path, *, warn: bool = True) -> LocationScopeConfig:
    scope = config.location_scope
    if not scope.enabled:
        return scope
    resolved = _load_geography(_location_scope_path(config, database), LocationScopeConfig, warn=warn)
    if resolved is None or scope.include_unrecognized:
        cache_config = config.model_copy(update={"paths": config.paths.model_copy(update={"database_path": database})})
        cached_scope = route_location_overrides(cache_config, write_cache=False, cache_only=True).location_scope
        if resolved is None or cached_scope.centers:
            return cached_scope
    resolved = resolved.model_copy(update={"include_unrecognized": scope.include_unrecognized})
    if not resolved.centers or resolved.region_names:
        return resolved
    cache_dir = database.parent / ".geography"
    region_codes = set()
    for location in config.search_locations:
        city = normalize_text(location.split(",", 1)[0])
        town = _load_geography(cache_dir / f"town-{city}.json", _Municipality, warn=warn)
        if town is None:
            return resolved
        region_codes.add(town.region_code)
    names = []
    for code in sorted(region_codes):
        region = _load_geography(cache_dir / f"region-{normalize_text(code)}.json", _Region, warn=warn)
        if region is None:
            return resolved
        names.append(region.name)
    return resolved.model_copy(update={"region_names": tuple(names)})


def save_location_scope(config: SearchConfig, resolved_scope: LocationScopeConfig) -> None:
    if not config.location_scope.enabled or not resolved_scope.centers:
        return
    _save_geography(_location_scope_path(config, config.database_path), resolved_scope)


def _fetch_municipality(client: httpx.Client, city: str) -> _Municipality:
    response = client.get(
        "/communes",
        params={
            "nom": city,
            "fields": "nom,centre,codeRegion,codeDepartement",
            "boost": "population",
            "limit": 10,
        },
    )
    response.raise_for_status()
    commune = next((item for item in response.json() if normalize_text(item["nom"]) == normalize_text(city)), None)
    if not commune:
        raise ValueError(f"Unknown French municipality: {city}")
    longitude, latitude = commune["centre"]["coordinates"]
    return _Municipality(
        center=LocationCenterConfig(name=commune["nom"], latitude=latitude, longitude=longitude),
        department=commune["codeDepartement"],
        region_code=commune["codeRegion"],
    )


def _fetch_region(client: httpx.Client, code: str) -> _Region:
    region = client.get(f"/regions/{code}", params={"fields": "nom"})
    region.raise_for_status()
    response = client.get("/communes", params={"codeRegion": code, "fields": "nom,centre"})
    response.raise_for_status()
    municipalities = []
    for town in response.json():
        if geometry := town.get("centre"):
            longitude, latitude = geometry["coordinates"]
            municipalities.append(LocationCenterConfig(name=town["nom"], latitude=latitude, longitude=longitude))
    return _Region(name=region.json()["nom"], municipalities=tuple(municipalities))


def _cached_geography[T: BaseModel](
    path: Path, model: type[T], fetch: Callable[[], T] | None, *, write_cache: bool
) -> T | None:
    cached = _load_geography(path, model, warn=fetch is not None)
    if cached is not None or fetch is None:
        return cached
    result = fetch()
    if write_cache:
        _save_geography(path, result)
    return result


def _load_geography[T: BaseModel](path: Path, model: type[T], *, warn: bool) -> T | None:
    try:
        return model.model_validate_json(path.read_bytes())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        if warn:
            log.warning("geography_cache_unreadable", path=str(path), error=str(exc))
        return None


def _save_geography(path: Path, data: BaseModel) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with TemporaryDirectory(prefix=".geography-", dir=path.parent) as directory:
            staged = Path(directory) / path.name
            staged.write_text(data.model_dump_json(), encoding="utf-8")
            staged.replace(path)
    except OSError as exc:
        log.warning("geography_cache_unwritable", path=str(path), error=str(exc))


def _location_scope_path(config: SearchConfig, database: Path) -> Path:
    inputs = (
        config.country,
        config.search_locations,
        config.location_scope.model_dump(mode="json", exclude={"region_names", "include_unrecognized"}),
    )
    digest = sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()[:16]
    return database.with_name(f"{database.name}.location-scope-{digest}.json")


def _direct_templates(
    source: str, templates: tuple[str, ...], cities: list[str], regions: tuple[str, ...]
) -> tuple[str, ...]:
    routed = []
    for template in templates:
        url = urlsplit(template)
        host = (url.hostname or "").removeprefix("www.")
        if source == "emploi_territorial" and host == "emploi-territorial.fr" and url.path == "/rechercher":
            routed.extend(_with_query_value(template, "search-region", code) for code in regions)
        elif source == "meteojob" and host == "meteojob.com" and url.path == "/jobsearch/offers":
            routed.extend(_with_query_value(template, "where", quote(city)) for city in cities)
        elif (
            source == "jobijoba"
            and host == "jobijoba.com"
            and re.fullmatch(r"/fr/emploi/\{query\}(?:/[^/]+)?", url.path)
        ):
            routed.extend(
                url._replace(path=f"/fr/emploi/{{query}}/{quote(city.replace(' ', '-'))}").geturl() for city in cities
            )
        elif source == "makesense" and host == "jobs.makesense.org" and url.path.startswith("/fr/s/jobs/all/"):
            routed.extend(
                template.replace("/fr/s/jobs/all/", f"/fr/s/jobs/{quote(city)}--France/", 1) for city in cities
            )
        else:
            routed.append(template)
    return tuple(dict.fromkeys(routed))


def _with_query_value(template: str, parameter: str, value: str) -> str:
    url = urlsplit(template)
    params = [part for part in url.query.split("&") if part and part.split("=", 1)[0] != parameter]
    return url._replace(query="&".join((*params, f"{parameter}={value}"))).geturl()
