from __future__ import annotations

import re
from urllib.parse import parse_qs, quote, quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from jobsfr.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from jobsfr.geography.scope import location_is_in_scope
from jobsfr.offers.filters import has_excluded_contract_signal
from jobsfr.offers.text import clean_text, normalize_text
from jobsfr.scrapers import cadremploi, france_travail
from jobsfr.scrapers.async_http import HostCooldownError, HostThrottle, get_checked_page
from jobsfr.scrapers.common import first_text

SITE_SEARCH_URL_TEMPLATES = {
    "apec": "https://www.apec.fr/candidat/recherche-emploi.html/emploi?q={query}&l={city_slug}-{department}&page=0",
    "optioncarriere": "https://www.optioncarriere.com/emploi-{query_slug}/{city}",
    "cadremploi": "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville={city_slug}-{department}",
    "isarta": "https://isarta.fr/cgi-bin/emplois/jobs?cat={region_slug}&query={query}",
}
SITE_SEARCH_DIRECT_PREFIXES = {
    "apec": ("/candidat/recherche-emploi.html/emploi/detail-offre/",),
    "optioncarriere": ("/jobad/",),
    "cadremploi": ("/emploi/detail_offre",),
    "isarta": ("/?job=",),
}
EMPLOI_COLLECTIVITES_SITEMAP = "https://www.emploi-collectivites.fr/XML/SiteMap_Offres0.xml"
SITEMAP_URL_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE)


async def _site_search_results(
    client: httpx.AsyncClient,
    spec: SiteSearchConfig,
    query: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
) -> list[str]:
    if spec.source in SITE_SEARCH_DIRECT_PREFIXES and spec.search_url_templates:
        return await _direct_site_search_results(
            client, spec.source, query, config, spec.search_url_templates, throttle
        )
    raise ValueError(f"No direct discovery URL configured for {spec.source}")


def site_search_templates(spec: SiteSearchConfig, location: str, config: SearchConfig) -> tuple[str, ...]:
    templates = spec.search_urls_by_location.get(location)
    if templates is None:
        templates = spec.search_url_templates
        if not templates:
            default = config.aggregators.site_url_templates.get(spec.source, SITE_SEARCH_URL_TEMPLATES.get(spec.source))
            templates = (default,) if default else ()
    city = location.split(",", 1)[0].strip()
    metadata = config.aggregators.site_locations.get(location) or config.aggregators.site_locations.get(
        f"{city}, France"
    )
    values = {
        "city": quote(city),
        "city_slug": normalize_text(city).replace(" ", "-"),
        "query": "{query}",
        "query_slug": "{query_slug}",
        "radius": str(config.distance_km),
    }
    if metadata:
        values.update(department=metadata.department, region_slug=metadata.region_slug)
    return tuple(
        template.format_map(values)
        for template in templates
        if metadata or not any(key in template for key in ("{department}", "{region_slug}"))
    )


async def _direct_site_search_results(
    client: httpx.AsyncClient,
    source: str,
    query: str,
    config: SearchConfig,
    search_url_templates: tuple[str, ...] | None = None,
    throttle: HostThrottle | None = None,
) -> list[str]:
    urls: list[str] = []
    regional_urls: set[str] = set()
    prefixes = SITE_SEARCH_DIRECT_PREFIXES[source]
    failures: list[str] = []
    successful_pages = 0
    if search_url_templates is None:
        search_url_templates = tuple(
            dict.fromkeys(
                template
                for spec in config.aggregators.site_search_sources
                if spec.source == source
                for location in config.search_locations
                for template in site_search_templates(spec, location, config)
            )
        )
    for template in search_url_templates:
        search_url = template.format(query=quote_plus(query), query_slug=quote(query.replace(" ", "-")))
        try:
            response = (
                await cadremploi.get_page(client, search_url, throttle)
                if source == "cadremploi"
                else await get_checked_page(client, search_url, throttle)
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            if isinstance(exc, httpx.HTTPStatusError):
                reason = f"HTTP {exc.response.status_code}"
            elif isinstance(exc, HostCooldownError):
                reason = str(exc)
            else:
                reason = type(exc).__name__
            failures.append(reason)
            continue
        if "text/html" not in response.headers.get("content-type", ""):
            failures.append("non-HTML response")
            continue
        successful_pages += 1
        soup = BeautifulSoup(response.text, "lxml")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href")
            if not isinstance(href, str):
                continue
            parsed = urlparse(urljoin(str(response.url), href))
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if not any(href.startswith(prefix) or path.startswith(prefix) for prefix in prefixes):
                continue
            url = urljoin(str(response.url), href.split("#", 1)[0])
            if url in urls:
                continue
            title = first_text(anchor, (".poste-listing-monopage",)) if source == "isarta" else None
            title = title or clean_text(anchor.get_text(" "))
            if not title or not config.filters.title.allows(title):
                continue
            if has_excluded_contract_signal(title, None, None):
                continue
            urls.append(url)
            if source == "isarta":
                location = first_text(anchor, (".lieu-listing-monopage",)) or title
                if location_is_in_scope(location, config.location_scope):
                    regional_urls.add(url)
    if not successful_pages:
        raise RuntimeError(f"{source} discovery failed: " + "; ".join(dict.fromkeys(failures)))
    if source == "isarta":
        urls.sort(key=lambda url: url not in regional_urls)
    return urls[: config.aggregators.max_detail_pages]


def _emploi_collectivites_sitemap_results(
    sitemap_urls: list[str],
    query: str,
    config: SearchConfig,
) -> list[str]:
    urls: list[str] = []
    include_terms = tuple(
        term.lower().replace(" ", "-") for term in config.filters.title.include if isinstance(term, str)
    )
    query_terms = tuple(part.lower() for part in query.split() if len(part) > 3)
    regional_terms = tuple(term.lower().replace(" ", "-") for term in config.aggregators.location_terms)
    for url in sitemap_urls:
        lowered = url.lower()
        if not any(term in lowered for term in include_terms + query_terms):
            continue
        if not any(term in lowered for term in regional_terms):
            continue
        urls.append(url)
        if len(urls) >= config.aggregators.max_detail_pages:
            break
    return urls


async def _detail_links(
    client: httpx.AsyncClient,
    spec: DirectAggregatorConfig,
    search_url: str,
    throttle: HostThrottle | None = None,
    config: SearchConfig | None = None,
    listing_dates: dict[str, str] | None = None,
) -> list[str]:
    response = await get_checked_page(client, search_url, throttle)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    urls: list[str] = []
    regional_urls: set[str] = set()
    is_makesense = spec.source == "makesense"
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        if not isinstance(href, str):
            continue
        if is_makesense and not urlparse(urljoin(spec.base_url, href)).path.startswith("/fr/jobs/"):
            continue
        if not any(href.startswith(prefix) or urlparse(href).path.startswith(prefix) for prefix in spec.link_prefixes):
            continue
        if config and spec.source in {"france_travail", "emploi_territorial", "meteojob", "makesense"}:
            title = first_text(anchor, ("h3.job__title",)) if is_makesense else None
            title = title or clean_text(anchor.get_text(" "))
            if not config.filters.title.allows(title) or has_excluded_contract_signal(title, None, None):
                continue
        url = urljoin(spec.base_url, href.split("#", 1)[0])
        if listing_dates is not None and spec.source == "france_travail":
            date_posted = france_travail.listing_date(anchor)
            if date_posted:
                listing_dates[url] = date_posted
        if url not in urls:
            urls.append(url)
        if config and is_makesense:
            location = first_text(anchor, ("address",))
            if location and location_is_in_scope(location, config.location_scope):
                regional_urls.add(url)
        elif len(urls) >= spec.max_links:
            break
    if regional_urls:
        urls.sort(key=lambda url: url not in regional_urls)
    return urls[: spec.max_links]


def _search_urls(spec: DirectAggregatorConfig, query: str, config: SearchConfig) -> list[str]:
    encoded_query = quote_plus(query)
    departments = config.aggregators.direct_locations if spec.use_direct_locations else (None,)
    urls = []
    for department in departments:
        for template in spec.search_url_templates:
            url = template.format(query=encoded_query, department=department or "", radius=config.distance_km)
            if spec.source == "makesense" and "s" not in parse_qs(urlparse(url).query, keep_blank_values=True):
                parsed = urlparse(url)
                if parsed.hostname == "jobs.makesense.org":
                    url = parsed._replace(query="&".join(filter(None, (parsed.query, f"s={encoded_query}")))).geturl()
            urls.append(url)
    return urls


def _site_result_matches_source(spec: SiteSearchConfig, url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return spec.host in host
