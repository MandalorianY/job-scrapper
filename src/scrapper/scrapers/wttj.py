from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import SupportsFloat, SupportsIndex, cast
from urllib.parse import urlencode

import httpx
import structlog
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers.async_http import HostThrottle, SourceBlockedError, get_checked_page
from scrapper.scrapers.common import (
    apply_url,
    custom_search_cutoff,
    json_ld_jobposting,
    offer_matches_search,
    posted_date_is_before_cutoff,
)
from scrapper.storage import JobStore
from scrapper.text import clean_text, is_application_destination

WTTJ_SOURCE = "welcome_to_the_jungle"
WINDOW_ENV_MARKER_RE = re.compile(r"window\.env\s*=\s*")
log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WttjAlgoliaConfig:
    api_key: str
    app_id: str
    url: str
    job_index: str


async def scrape_welcome_to_the_jungle(config: SearchConfig, store: JobStore) -> int:
    if not config.is_source_enabled(WTTJ_SOURCE) or not config.queries:
        return 0

    log.info(
        "wttj_scrape_started",
        source=WTTJ_SOURCE,
        queries=list(config.queries),
        centers=[center_name for center_name, _center_lat_lng in _search_centers(config)],
        max_pages=config.custom_max_pages,
    )
    configured_key = config.welcome_to_the_jungle.algolia_api_key
    configured_value = configured_key.get_secret_value().strip() if configured_key else ""
    throttle = HostThrottle(config.http_per_host_concurrency, config.http_request_delay_secs)
    try:
        discovered = await _public_algolia_config(config, throttle)
    except SourceBlockedError as exc:
        query = config.queries[0]
        center_name, _ = _search_centers(config)[0]
        store.start_search(WTTJ_SOURCE, query, center_name)
        store.finish_search(WTTJ_SOURCE, query, center_name, 0, error=str(exc))
        return 0
    algolia_api_key = configured_value or (discovered.api_key if discovered else "")
    algolia_app_id = _configured_or_discovered(
        config.welcome_to_the_jungle.algolia_app_id,
        discovered.app_id if discovered else None,
    )
    algolia_url = _configured_or_discovered(
        config.welcome_to_the_jungle.algolia_url,
        discovered.url if discovered else None,
    )
    job_index = _configured_or_discovered(
        config.welcome_to_the_jungle.job_index,
        discovered.job_index if discovered else None,
    )
    if not (algolia_api_key and algolia_app_id and algolia_url and job_index):
        error = "Welcome to the Jungle Algolia runtime config is not available."
        for query in config.queries:
            for center_name, _center_lat_lng in _search_centers(config):
                store.start_search(WTTJ_SOURCE, query, center_name)
                store.finish_search(
                    WTTJ_SOURCE,
                    query,
                    center_name,
                    0,
                    error=error,
                )
        log.error("wttj_scrape_failed", source=WTTJ_SOURCE, error=error)
        return 0

    written = 0
    processed_urls: set[str] = set()
    headers = {
        "x-algolia-application-id": algolia_app_id,
        "x-algolia-api-key": algolia_api_key,
        "content-type": "application/x-www-form-urlencoded",
        "accept": "*/*",
        "origin": config.welcome_to_the_jungle.site_url,
        "referer": f"{config.welcome_to_the_jungle.site_url}/",
        "user-agent": config.user_agent,
    }
    limits = httpx.Limits(
        max_connections=config.http_max_connections, max_keepalive_connections=config.http_max_connections
    )
    async with (
        httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True, limits=limits) as client,
        httpx.AsyncClient(timeout=15, follow_redirects=True) as detail_client,
    ):
        for query in config.queries:
            for center_name, center_lat_lng in _search_centers(config):
                store.start_search(WTTJ_SOURCE, query, center_name)
                seen = 0
                try:
                    geo_params: dict[str, str | int] = {}
                    if center_lat_lng:
                        geo_params["aroundLatLng"] = center_lat_lng
                        geo_params["aroundRadius"] = (
                            config.welcome_to_the_jungle.around_radius or config.distance_km * 1000
                        )
                    cutoff = custom_search_cutoff(
                        store,
                        WTTJ_SOURCE,
                        query,
                        center_name,
                        config,
                    )
                    for page in range(config.custom_max_pages):
                        payload = {
                            "requests": [
                                {
                                    "indexName": job_index,
                                    "params": urlencode(
                                        {
                                            "query": query,
                                            "hitsPerPage": min(config.results_wanted_per_query, 50),
                                            "page": page,
                                            "facetFilters": config.welcome_to_the_jungle.office_country_filter,
                                            **geo_params,
                                        },
                                        safe=':,/[]"',
                                    ),
                                }
                            ]
                        }
                        response = await throttle.post(
                            client,
                            algolia_url,
                            content=json.dumps(payload),
                        )
                        response.raise_for_status()
                        result = response.json()["results"][0]
                        hits = result.get("hits", [])
                        if not hits:
                            break
                        for hit in hits:
                            seen += 1
                            if posted_date_is_before_cutoff(hit.get("published_at"), cutoff):
                                # Algolia ranks by relevance, so later hits can still be recent.
                                continue
                            offer = _hit_to_offer(hit, query, config)
                            offer.search_location = center_name
                            if not offer.source_url or offer.source_url in processed_urls:
                                continue
                            if not offer_matches_search(offer, config, require_details=False):
                                continue
                            if not config.refresh_known_details and store.source_url_seen(
                                WTTJ_SOURCE, offer.source_url
                            ):
                                continue
                            await _enrich_public_detail(detail_client, throttle, offer)
                            if offer_matches_search(offer, config):
                                store.upsert_offer(offer)
                                written += 1
                            processed_urls.add(offer.source_url)
                    store.finish_search(WTTJ_SOURCE, query, center_name, seen)
                    log.info(
                        "wttj_search_finished",
                        source=WTTJ_SOURCE,
                        query=query,
                        location=center_name,
                        seen=seen,
                        stored=written,
                    )
                except Exception as exc:
                    error = str(exc) or type(exc).__name__
                    store.finish_search(
                        WTTJ_SOURCE,
                        query,
                        center_name,
                        seen,
                        error=error,
                    )
                    if isinstance(exc, SourceBlockedError):
                        return written
                    log.exception(
                        "wttj_search_failed",
                        source=WTTJ_SOURCE,
                        query=query,
                        location=center_name,
                        seen=seen,
                        error=error,
                    )
    log.info("wttj_scrape_completed", source=WTTJ_SOURCE, stored=written)
    return written


def _search_centers(config: SearchConfig) -> tuple[tuple[str, str | None], ...]:
    if config.location_scope.centers:
        return tuple((center.name, f"{center.latitude},{center.longitude}") for center in config.location_scope.centers)
    return ((config.location_label, None),)


async def _public_algolia_config(
    config: SearchConfig, throttle: HostThrottle | None = None
) -> WttjAlgoliaConfig | None:
    url = f"{config.welcome_to_the_jungle.site_url}/fr/sitemap.xml"
    # Keep httpx's default user agent because a bare Mozilla/5.0 header receives a 403.
    headers = {
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
        try:
            response = await get_checked_page(client, url, throttle)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
    env = _extract_window_env(response.text)
    if not env:
        return None
    api_key = clean_text(env.get("ALGOLIA_API_KEY_CLIENT"))
    app_id = clean_text(env.get("ALGOLIA_APPLICATION_ID"))
    jobs_prefix = clean_text(env.get("ALGOLIA_JOBS_INDEX_PREFIX"))
    if not (api_key and app_id and jobs_prefix):
        return None
    return WttjAlgoliaConfig(
        api_key=api_key,
        app_id=app_id,
        url=(
            f"https://{app_id}-dsn.algolia.net/1/indexes/*/queries"
            "?x-algolia-agent=Algolia%20for%20JavaScript%20(4.20.0)%3B%20Browser"
            "&search_origin=jobs_search_client"
        ),
        job_index=f"{jobs_prefix}_fr",
    )


def _extract_window_env(text: str) -> dict[str, object] | None:
    marker = WINDOW_ENV_MARKER_RE.search(text)
    if not marker:
        return None
    start = text.find("{", marker.end())
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index, char in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return parsed if isinstance(parsed, dict) else None
    return None


def _configured_or_discovered(configured: str | None, discovered: str | None) -> str | None:
    raw_value = configured or ""
    value = clean_text(raw_value)
    if value and not _is_placeholder(raw_value) and not _is_placeholder(value):
        return value
    return clean_text(discovered)


def _is_placeholder(value: str) -> bool:
    normalized = value.lower()
    return "<" in value or ">" in value or normalized.startswith("example_")


def _hit_to_offer(hit: dict[str, object], query: str, config: SearchConfig) -> JobOffer:
    org = hit.get("organization")
    org = org if isinstance(org, dict) else {}
    office = hit.get("office")
    offices = hit.get("offices")
    if not office and isinstance(offices, list) and offices:
        office = offices[0]
    office = office if isinstance(office, dict) else {}
    org_slug = org.get("slug") or ""
    job_slug = hit.get("slug") or ""
    source_url = (
        f"{config.welcome_to_the_jungle.site_url}/fr/companies/{org_slug}/jobs/{job_slug}"
        if org_slug and job_slug
        else ""
    )
    contract_names = hit.get("contract_type_names")
    contract_names = contract_names if isinstance(contract_names, dict) else {}
    salary = _salary(hit)
    company_description = _localized((org.get("descriptions") or {}), "fr") or _localized(
        (org.get("descriptions") or {}),
        "en",
    )
    missions = hit.get("key_missions") or ()
    if isinstance(missions, str):
        missions = (missions,)
    elif not isinstance(missions, (list, tuple)):
        missions = ()
    description_parts = (
        clean_text(hit.get("summary")),
        *(clean_text(mission) for mission in missions),
        clean_text(hit.get("profile")),
    )
    description = "\n\n".join(dict.fromkeys(filter(None, description_parts))) or None
    return JobOffer(
        source=WTTJ_SOURCE,
        source_id=clean_text(hit.get("objectID") or hit.get("reference")),
        source_url=source_url,
        direct_url=_direct_url(hit),
        title=clean_text(hit.get("name")),
        company=clean_text(org.get("name")),
        location=_location(office),
        description=description,
        date_posted=clean_text(hit.get("published_at")),
        employment_type=clean_text(contract_names.get("fr") or hit.get("contract_type")),
        salary=salary,
        min_amount=_number(hit.get("salary_minimum") or hit.get("salary_yearly_minimum")),
        max_amount=_number(hit.get("salary_maximum")),
        currency=clean_text(hit.get("salary_currency")),
        interval=clean_text(hit.get("salary_period")),
        is_remote=_is_remote(hit.get("remote")),
        company_url=f"{config.welcome_to_the_jungle.site_url}/fr/companies/{org_slug}" if org_slug else None,
        company_description=company_description,
        search_query=query,
        search_location=config.location_label,
        scraped_at=datetime.now(UTC).isoformat(),
        raw=hit,
    )


async def _enrich_public_detail(client: httpx.AsyncClient, throttle: HostThrottle, offer: JobOffer) -> None:
    response = await throttle.get(client, offer.source_url)
    if response.status_code in {403, 404, 410}:
        return
    response.raise_for_status()
    if response.status_code != 200 or "text/html" not in response.headers.get("content-type", ""):
        return
    soup = BeautifulSoup(response.text, "lxml")
    job = json_ld_jobposting(soup)
    if job:
        offer.description = clean_text(job.get("description")) or offer.description
        offer.direct_url = offer.direct_url or apply_url(soup, offer.source_url)


def _localized(value: object, locale: str) -> str | None:
    localized = value.get(locale) if isinstance(value, dict) else None
    if isinstance(localized, dict):
        return clean_text(localized.get("body") or localized.get("text"))
    return clean_text(localized)


def _direct_url(hit: dict[str, object]) -> str | None:
    for key in ("apply_url", "application_url", "external_apply_url", "redirect_url"):
        value = clean_text(hit.get(key))
        if is_application_destination(value):
            return value
    return None


def _location(office: dict[str, object]) -> str | None:
    return clean_text(
        ", ".join(
            str(part)
            for part in (
                office.get("city"),
                office.get("district"),
                office.get("state"),
                office.get("country"),
            )
            if part
        )
    )


def _salary(hit: dict[str, object]) -> str | None:
    minimum = hit.get("salary_minimum") or hit.get("salary_yearly_minimum")
    maximum = hit.get("salary_maximum")
    currency = hit.get("salary_currency")
    period = hit.get("salary_period")
    if minimum and maximum:
        return clean_text(f"{minimum} - {maximum} {currency or ''} {period or ''}")
    if minimum:
        return clean_text(f"{minimum} {currency or ''} {period or ''}")
    return None


def _number(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(cast("str | bytes | bytearray | SupportsFloat | SupportsIndex", value))
    except (TypeError, ValueError):
        return None


def _is_remote(value: object) -> bool | None:
    if not isinstance(value, str):
        return None
    if value in {"fulltime", "partial", "punctual"}:
        return True
    if value == "no":
        return False
    return None
