from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore
from scrapper.text import clean_text, contains_any_terms, has_excluded_contract_signal


PUBLIC_ALGOLIA_KEY_RE = re.compile(r'"ALGOLIA_API_KEY_CLIENT"\s*:\s*"([^"]+)"')


async def scrape_welcome_to_the_jungle(config: SearchConfig, store: JobStore) -> int:
    if not config.is_source_enabled(config.welcome_to_the_jungle.source):
        return 0

    configured_key = config.welcome_to_the_jungle.algolia_api_key
    configured_value = configured_key.get_secret_value().strip() if configured_key else ""
    algolia_api_key = configured_value or await _public_algolia_api_key(config)
    if not algolia_api_key:
        for query in config.queries:
            for center_name, _center_lat_lng in _search_centers(config):
                store.start_search(config.welcome_to_the_jungle.source, query, center_name)
                store.finish_search(
                    config.welcome_to_the_jungle.source,
                    query,
                    center_name,
                    0,
                    error="Welcome to the Jungle Algolia API key is not configured.",
                )
        return 0

    written = 0
    headers = {
        "x-algolia-application-id": config.welcome_to_the_jungle.algolia_app_id,
        "x-algolia-api-key": algolia_api_key,
        "content-type": "application/x-www-form-urlencoded",
        "accept": "*/*",
        "origin": config.welcome_to_the_jungle.site_url,
        "referer": f"{config.welcome_to_the_jungle.site_url}/",
        "user-agent": config.user_agent,
    }
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for query in config.queries:
            for center_name, center_lat_lng in _search_centers(config):
                store.start_search(config.welcome_to_the_jungle.source, query, center_name)
                seen = 0
                try:
                    for page in range(config.custom_max_pages):
                        payload = {
                            "requests": [
                                {
                                    "indexName": config.welcome_to_the_jungle.job_index,
                                    "params": urlencode(
                                        {
                                            "query": query,
                                            "hitsPerPage": min(config.results_wanted_per_query, 50),
                                            "page": page,
                                            "aroundLatLng": center_lat_lng,
                                            "aroundRadius": config.welcome_to_the_jungle.around_radius,
                                            "facetFilters": config.welcome_to_the_jungle.office_country_filter,
                                        },
                                        safe=':,/[]"',
                                    ),
                                }
                            ]
                        }
                        response = await client.post(config.welcome_to_the_jungle.algolia_url, content=json.dumps(payload))
                        response.raise_for_status()
                        result = response.json()["results"][0]
                        hits = result.get("hits", [])
                        if not hits:
                            break
                        for hit in hits:
                            seen += 1
                            offer = _hit_to_offer(hit, query, config)
                            offer.search_location = center_name
                            if not offer.source_url:
                                continue
                            if has_excluded_contract_signal(
                                offer.title,
                                offer.employment_type,
                                offer.description,
                                config.exclude_terms,
                            ):
                                continue
                            if not contains_any_terms((offer.title,), config.include_terms):
                                continue
                            if not location_is_in_scope(offer.location, config.location_scope):
                                continue
                            if (
                                not config.refresh_known_details
                                and store.source_url_seen(config.welcome_to_the_jungle.source, offer.source_url)
                            ):
                                continue
                            store.upsert_offer(offer)
                            written += 1
                    store.finish_search(config.welcome_to_the_jungle.source, query, center_name, seen)
                except Exception as exc:  # noqa: BLE001 - keep the other sources running.
                    store.finish_search(
                        config.welcome_to_the_jungle.source,
                        query,
                        center_name,
                        seen,
                        error=str(exc),
                    )
    return written


def _search_centers(config: SearchConfig) -> tuple[tuple[str, str], ...]:
    if config.location_scope.enabled and config.location_scope.centers:
        return tuple(
            (center.name, f"{center.latitude},{center.longitude}")
            for center in config.location_scope.centers
        )
    return ((config.location_label, config.welcome_to_the_jungle.around_lat_lng),)


async def _public_algolia_api_key(config: SearchConfig) -> str | None:
    url = f"{config.welcome_to_the_jungle.site_url}/fr/sitemap.xml"
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15, follow_redirects=True) as client:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError:
            return None
    match = PUBLIC_ALGOLIA_KEY_RE.search(response.text)
    return match.group(1) if match else None


def _hit_to_offer(hit: dict, query: str, config: SearchConfig) -> JobOffer:
    org = hit.get("organization") or {}
    office = hit.get("office") or _first(hit.get("offices")) or {}
    org_slug = org.get("slug") or ""
    job_slug = hit.get("slug") or ""
    source_url = (
        f"{config.welcome_to_the_jungle.site_url}/fr/companies/{org_slug}/jobs/{job_slug}"
        if org_slug and job_slug
        else ""
    )
    contract_names = hit.get("contract_type_names") or {}
    salary = _salary(hit)
    company_description = _localized((org.get("descriptions") or {}), "fr") or _localized(
        (org.get("descriptions") or {}),
        "en",
    )
    description = clean_text(hit.get("profile")) or company_description
    return JobOffer(
        source=config.welcome_to_the_jungle.source,
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
        scraped_at=datetime.now(timezone.utc).isoformat(),
        raw=hit,
    )


def _localized(value: dict, locale: str) -> str | None:
    localized = value.get(locale)
    if isinstance(localized, dict):
        return clean_text(localized.get("body") or localized.get("text"))
    return clean_text(localized)


def _direct_url(hit: dict) -> str | None:
    for key in ("apply_url", "application_url", "external_apply_url", "redirect_url", "website"):
        value = clean_text(hit.get(key))
        if value and value.startswith(("http://", "https://")):
            return value
    return None


def _location(office: dict) -> str | None:
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


def _salary(hit: dict) -> str | None:
    minimum = hit.get("salary_minimum") or hit.get("salary_yearly_minimum")
    maximum = hit.get("salary_maximum")
    currency = hit.get("salary_currency")
    period = hit.get("salary_period")
    if minimum and maximum:
        return clean_text(f"{minimum} - {maximum} {currency or ''} {period or ''}")
    if minimum:
        return clean_text(f"{minimum} {currency or ''} {period or ''}")
    return None


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(value):
    if isinstance(value, list) and value:
        return value[0]
    return None


def _is_remote(value) -> bool | None:
    if value in {"fulltime", "partial", "punctual"}:
        return True
    if value == "no":
        return False
    return None
