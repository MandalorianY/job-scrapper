import re
from urllib.parse import quote, urlsplit

import httpx

from scrapper.config import SearchConfig
from scrapper.config.settings import LocationCenterConfig, SiteLocationConfig
from scrapper.location_scope import coordinates_are_in_scope
from scrapper.text import normalize_text

GEO_API = "https://geo.api.gouv.fr"


def route_location_overrides(config: SearchConfig) -> SearchConfig:
    if config.country.casefold() != "france":
        return config
    cities = []
    site_locations = dict(config.aggregators.site_locations)
    regions = {}
    centers = []
    coordinates = dict(config.location_scope.city_coordinates)
    with httpx.Client(base_url=GEO_API, timeout=10) as client:
        for location in config.search_locations:
            city = location.split(",", 1)[0].strip()
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
            commune = next(
                (item for item in response.json() if normalize_text(item["nom"]) == normalize_text(city)), None
            )
            if not commune:
                raise ValueError(f"Unknown French municipality: {city}")
            region = commune["codeRegion"]
            if region not in regions:
                response = client.get(f"/regions/{region}", params={"fields": "nom"})
                response.raise_for_status()
                regions[region] = response.json()["nom"]
            longitude, latitude = commune["centre"]["coordinates"]
            configured_center = next(
                (
                    center
                    for center in config.location_scope.centers
                    if normalize_text(center.name) == normalize_text(city)
                ),
                None,
            )
            centers.append(
                configured_center
                or LocationCenterConfig(
                    name=commune["nom"],
                    latitude=latitude,
                    longitude=longitude,
                )
            )
            cities.append(commune["nom"])
            site_locations.setdefault(
                location,
                SiteLocationConfig(
                    department=commune["codeDepartement"],
                    region_slug=normalize_text(regions[region]).replace(" ", "-"),
                ),
            )

        for region in regions:
            response = client.get("/communes", params={"codeRegion": region, "fields": "nom,centre"})
            response.raise_for_status()
            for commune in response.json():
                if commune.get("centre"):
                    longitude, latitude = commune["centre"]["coordinates"]
                    coordinates.setdefault(commune["nom"], (latitude, longitude))

    scope = config.location_scope.model_copy(update={"centers": tuple(centers)})
    # Resolve nearby municipalities once so repeated offer checks use a small lookup.
    local_coordinates = {city: point for city, point in coordinates.items() if coordinates_are_in_scope(point, scope)}
    regional_names = tuple(sorted((city.casefold() for city in local_coordinates), key=len, reverse=True))
    scope = scope.model_copy(update={"city_coordinates": local_coordinates})
    return config.model_copy(
        update={
            "location_scope": scope,
            "aggregators": config.aggregators.model_copy(
                update={
                    "location_terms": tuple(dict.fromkeys((*regional_names, *config.aggregators.location_terms))),
                    "direct_locations": tuple(f"{code}R" for code in regions),
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
        elif source == "jobijoba" and host == "jobijoba.com" and re.fullmatch(r"/fr/emploi/\{query\}/[^/]+", url.path):
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
