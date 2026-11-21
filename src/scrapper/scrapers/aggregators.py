from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import TypeGuard
from urllib.parse import parse_qs, quote, quote_plus, unquote, urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from scrapper.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from scrapper.dates import FRENCH_DATE_PHRASE_RE
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.scrapers import cadremploi, france_travail
from scrapper.scrapers.async_http import HostThrottle
from scrapper.scrapers.common import (
    apply_url,
    custom_search_cutoff,
    first_text,
    job_location,
    json_ld_jobposting,
    meta_content,
    org_name,
    posted_date_is_before_cutoff,
)
from scrapper.source_rules import get_source_rules
from scrapper.source_rules.meteojob import is_placeholder_offer
from scrapper.storage import JobStore
from scrapper.text import clean_text, external_url_key, has_excluded_contract_signal, normalize_text

log = structlog.get_logger(__name__)

AGGREGATORS_SOURCE = "aggregators"
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


async def scrape_aggregators(config: SearchConfig, store: JobStore) -> dict[str, int]:
    if not config.is_source_enabled(AGGREGATORS_SOURCE):
        return {}

    counts: dict[str, int] = {}
    log.info(
        "aggregators_scrape_started",
        direct_sources=[
            spec.source for spec in config.aggregators.direct_specs if config.is_source_enabled(spec.source)
        ],
        site_search_sources=[
            spec.source for spec in config.aggregators.site_search_sources if config.is_source_enabled(spec.source)
        ],
    )
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}
    limits = httpx.Limits(
        max_connections=config.http_max_connections, max_keepalive_connections=config.http_max_connections
    )
    throttle = HostThrottle(config.http_per_host_concurrency, config.http_request_delay_secs)
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True, limits=limits) as client:
        direct_tasks = [
            _scrape_direct_spec(client, spec, config, store, throttle)
            for spec in config.aggregators.direct_specs
            if config.is_source_enabled(spec.source)
        ]
        direct_results = await asyncio.gather(*direct_tasks)
        for spec, count in zip(
            (spec for spec in config.aggregators.direct_specs if config.is_source_enabled(spec.source)),
            direct_results,
            strict=False,
        ):
            counts[spec.source] = count
        site_counts = await _scrape_site_search(client, config, store, throttle)
        counts.update(site_counts)
    log.info("aggregators_scrape_completed", counts=counts)
    return counts


async def _scrape_direct_spec(
    client: httpx.AsyncClient,
    spec: DirectAggregatorConfig,
    config: SearchConfig,
    store: JobStore,
    throttle: HostThrottle | None = None,
) -> int:
    written = 0
    for query in config.queries:
        checked_urls: set[str] = set()
        listing_dates: dict[str, str] = {}
        store.start_search(spec.source, query, config.location_label)
        seen_count = 0
        try:
            cutoff = custom_search_cutoff(store, spec.source, query, config.location_label, config)
            for search_url in _search_urls(spec, query, config):
                checked_on_page = 0
                for detail_url in await _detail_links(
                    client,
                    spec,
                    search_url,
                    throttle,
                    config=config,
                    listing_dates=listing_dates,
                ):
                    if checked_on_page >= config.aggregators.max_detail_pages:
                        break
                    if detail_url in checked_urls or (
                        not config.refresh_known_details and store.source_url_seen(spec.source, detail_url)
                    ):
                        continue
                    checked_urls.add(detail_url)
                    checked_on_page += 1
                    seen_count += 1
                    offer = await _extract_detail_offer(
                        client, detail_url, spec.source, query, config, throttle=throttle
                    )
                    if offer and not offer.date_posted:
                        offer.date_posted = listing_dates.get(detail_url)
                    # Relevance-ordered pages can contain newer offers after an old one.
                    if not _offer_is_usable(offer, config, cutoff=cutoff):
                        continue
                    store.upsert_offer(offer)
                    written += 1
            store.finish_search(spec.source, query, config.location_label, seen_count)
            log.info(
                "aggregator_direct_search_finished",
                source=spec.source,
                query=query,
                location=config.location_label,
                seen=seen_count,
                stored=written,
            )
        except Exception as exc:
            store.finish_search(spec.source, query, config.location_label, seen_count, error=str(exc))
            log.exception(
                "aggregator_direct_search_failed",
                source=spec.source,
                query=query,
                location=config.location_label,
                seen=seen_count,
            )
    return written


async def _scrape_site_search(
    client: httpx.AsyncClient,
    config: SearchConfig,
    store: JobStore,
    throttle: HostThrottle | None = None,
) -> dict[str, int]:
    counts = {
        spec.source: 0 for spec in config.aggregators.site_search_sources if config.is_source_enabled(spec.source)
    }
    for spec in config.aggregators.site_search_sources:
        if not config.is_source_enabled(spec.source):
            continue
        for query in config.queries:
            for search_location in config.search_locations:
                search_query = spec.query_template.format(query=query, location=search_location)
                store.start_search(spec.source, search_query, search_location)
                seen = 0
                try:
                    cutoff = custom_search_cutoff(store, spec.source, search_query, search_location, config)
                    templates = site_search_templates(spec, search_location, config)
                    search_spec = spec.model_copy(update={"search_url_templates": templates})
                    urls = await _site_search_results(client, search_spec, query, config)
                    candidates: list[str] = []
                    for url in urls:
                        seen += 1
                        if not _site_result_matches_source(spec, url):
                            continue
                        if not config.refresh_known_details and store.source_url_seen(spec.source, url):
                            continue
                        candidates.append(url)
                    offers = await asyncio.gather(
                        *(
                            _extract_detail_offer(
                                client,
                                url,
                                spec.source,
                                search_query,
                                config,
                                throttle=throttle,
                            )
                            for url in candidates
                        )
                    )
                    for offer in offers:
                        if not _offer_is_usable(offer, config, cutoff=cutoff):
                            continue
                        offer.search_location = search_location
                        store.upsert_offer(offer)
                        counts[spec.source] += 1
                    store.finish_search(spec.source, search_query, search_location, seen)
                    log.info(
                        "aggregator_site_search_finished",
                        source=spec.source,
                        query=search_query,
                        location=search_location,
                        seen=seen,
                        candidates=len(candidates),
                        stored=counts[spec.source],
                    )
                except Exception as exc:
                    store.finish_search(spec.source, search_query, search_location, seen, error=str(exc))
                    log.exception(
                        "aggregator_site_search_failed",
                        source=spec.source,
                        query=search_query,
                        location=search_location,
                        seen=seen,
                    )
    return counts


async def _site_search_results(
    client: httpx.AsyncClient,
    spec: SiteSearchConfig,
    query: str,
    config: SearchConfig,
) -> list[str]:
    if spec.source == "emploi_collectivites":
        return await _emploi_collectivites_sitemap_results(client, query, config)
    if spec.source in SITE_SEARCH_DIRECT_PREFIXES and spec.search_url_templates:
        return await _direct_site_search_results(client, spec.source, query, config, spec.search_url_templates)
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
                await cadremploi.get_page(client, search_url)
                if source == "cadremploi"
                else await client.get(search_url)
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            reason = (
                f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            )
            failures.append(reason)
            continue
        if "text/html" not in response.headers.get("content-type", ""):
            failures.append("non-HTML response")
            continue
        if any(marker in response.text for marker in ("captcha.careerjet.net", "captcha-delivery.com")):
            failures.append("verification challenge")
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
            if not title or not config.title_is_allowed(title):
                continue
            if has_excluded_contract_signal(title, None, None, config.exclude_terms):
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


async def _emploi_collectivites_sitemap_results(
    client: httpx.AsyncClient,
    query: str,
    config: SearchConfig,
) -> list[str]:
    response = await client.get(EMPLOI_COLLECTIVITES_SITEMAP)
    response.raise_for_status()
    urls: list[str] = []
    include_terms = tuple(term.lower().replace(" ", "-") for term in config.include_terms)
    query_terms = tuple(part.lower() for part in query.split() if len(part) > 3)
    regional_terms = tuple(term.lower().replace(" ", "-") for term in config.aggregators.location_terms)
    for url in SITEMAP_URL_RE.findall(response.text):
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
    response = await throttle.get(client, search_url) if throttle else await client.get(search_url)
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
            if not config.title_is_allowed(title) or has_excluded_contract_signal(
                title, None, None, config.exclude_terms
            ):
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


async def _extract_detail_offer(
    client: httpx.AsyncClient,
    url: str,
    source: str,
    query: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
) -> JobOffer | None:
    if source == "france_travail":
        return await france_travail.extract_public_offer(client, url, query, config, throttle)
    detail_url = url
    if source == "isarta":
        job_id = parse_qs(urlparse(url).query).get("job", [""])[0]
        if job_id.isdigit():
            detail_url = f"https://isarta.fr/cgi-bin/emplois/pm.cgi?action=display&login={job_id}"
    try:
        if source == "cadremploi":
            response = await cadremploi.get_page(client, detail_url, throttle)
        else:
            response = await throttle.get(client, detail_url) if throttle else await client.get(detail_url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
        return None
    if _page_provider_is_blocked(source, response.text, config):
        return None

    soup = BeautifulSoup(response.text, "lxml")
    job = json_ld_jobposting(soup) or {}
    text = soup.get_text("\n", strip=True)
    source_rules = get_source_rules(source)
    title = source_rules.normalize_title(
        clean_text(job.get("title"))
        or source_rules.extract_title(soup, url)
        or _clean_title(first_text(soup, ("h1",)))
        or meta_content(soup, "og:title", "twitter:title")
    )
    company = (
        source_rules.extract_company(soup, url)
        or org_name(job.get("hiringOrganization"))
        or _company_from_page(source, soup, text, url)
    )
    location = (
        source_rules.extract_location(soup, url)
        or job_location(job.get("jobLocation"))
        or _infer_location(text, config)
    )
    source_description = source_rules.extract_description(soup, url)
    if source == "emploi_collectivites" and not source_description:
        return None
    description = source_description or clean_text(job.get("description")) or _description_from_page(soup)
    employment_type = source_rules.extract_employment_type(soup, url) or _employment_type(job.get("employmentType"))
    if not employment_type and source != "jobijoba":
        employment_type = _infer_contract(description)
    direct_url = await _direct_url_for_detail(client, soup, url, source, throttle)
    if _direct_url_is_blocked(source, direct_url, config):
        return None

    return JobOffer(
        source=source,
        source_id=_offer_source_id(source, url, direct_url),
        source_url=url,
        direct_url=direct_url or url,
        title=title,
        company=company,
        location=location,
        description=description,
        date_posted=source_rules.extract_date(soup, url) or clean_text(job.get("datePosted")) or _infer_date(text),
        employment_type=employment_type,
        salary=clean_text(job.get("baseSalary")) or source_rules.extract_salary(soup, url) or _infer_salary(text),
        company_url=source_rules.extract_company_url(soup, url) or _site_root(url),
        search_query=query,
        search_location=config.location_label,
        scraped_at=datetime.now(UTC).isoformat(),
        raw={"json_ld": job, "url": url},
    )


async def _direct_url_for_detail(
    client: httpx.AsyncClient,
    soup: BeautifulSoup,
    url: str,
    source: str,
    throttle: HostThrottle | None = None,
) -> str | None:
    direct_url = apply_url(soup, url)
    if source != "jobijoba" or not direct_url:
        return direct_url
    resolved = await _jobijoba_redirect_target(client, direct_url, throttle)
    return resolved or direct_url


async def _jobijoba_redirect_target(
    client: httpx.AsyncClient,
    redirect_url: str,
    throttle: HostThrottle | None = None,
) -> str | None:
    try:
        response = await throttle.get(client, redirect_url) if throttle else await client.get(redirect_url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    response_target = _jobijoba_external_target(str(response.url))
    if response_target:
        return response_target
    if "text/html" not in response.headers.get("content-type", ""):
        return None
    soup = BeautifulSoup(response.text, "lxml")
    for anchor in soup.select("a[href]"):
        href = anchor.get("href")
        target = _jobijoba_external_target(href) if isinstance(href, str) else None
        if target:
            return target
    for match in re.findall(r"https?://[^\"'<> ]+", response.text):
        target = _jobijoba_external_target(match)
        if target:
            return target
    return None


def _jobijoba_external_target(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "jobijoba.com" in host:
        return None
    from_url = parse_qs(parsed.query).get("from", [None])[0]
    if from_url:
        return unquote(from_url)
    if parsed.scheme in {"http", "https"} and host:
        return url
    return None


def _direct_url_is_blocked(source: str, direct_url: str | None, config: SearchConfig) -> bool:
    if not direct_url:
        return False
    spec = next((item for item in config.aggregators.direct_specs if item.source == source), None)
    if not spec or not spec.blocked_direct_hosts:
        return False
    host = (urlparse(direct_url).hostname or "").lower().rstrip(".")
    return any(
        host == blocked.lower().rstrip(".") or host.endswith(f".{blocked.lower().rstrip('.')}")
        for blocked in spec.blocked_direct_hosts
    )


def _page_provider_is_blocked(source: str, html: str, config: SearchConfig) -> bool:
    spec = next((item for item in config.aggregators.direct_specs if item.source == source), None)
    if not spec or not spec.blocked_provider_terms:
        return False
    detail = re.search(
        r"['\"]detail['\"]\s*:\s*\{.*?['\"]customerName['\"]\s*:\s*['\"]([^'\"]+)",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not detail:
        return False
    provider = detail.group(1).casefold()
    return any(term.casefold() in provider for term in spec.blocked_provider_terms)


def _offer_is_usable(
    offer: JobOffer | None,
    config: SearchConfig,
    *,
    cutoff: datetime | None = None,
) -> TypeGuard[JobOffer]:
    if not offer or not offer.title:
        return False
    if cutoff is not None:
        public_offer = (offer.raw or {}).get("public_offer")
        modified = public_offer.get("dateDerniereModification") if isinstance(public_offer, dict) else None
        date = offer.date_posted or modified
        if posted_date_is_before_cutoff(date, cutoff):
            return False
    if _is_meteojob_placeholder_offer(offer):
        return False
    if has_excluded_contract_signal(offer.title, offer.employment_type, offer.description, config.exclude_terms):
        return False
    if not config.title_is_allowed(offer.title):
        return False
    if config.is_company_excluded(offer.source, offer.company):
        return False
    return _has_regional_signal(offer.location, offer.description, config)


def _is_meteojob_placeholder_offer(offer: JobOffer) -> bool:
    if getattr(offer, "source", None) != "meteojob":
        return False
    raw_value = getattr(offer, "raw", None)
    raw = raw_value if isinstance(raw_value, dict) else {}
    return is_placeholder_offer(
        getattr(offer, "title", None),
        getattr(offer, "company", None),
        getattr(offer, "date_posted", None),
        has_jobposting=bool(raw.get("json_ld")),
    )


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


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    parts = [part.strip() for part in title.split("\n") if part.strip()]
    return parts[-1] if parts else title


def _company_from_page(source: str, soup: BeautifulSoup, text: str, url: str) -> str | None:
    if source == "example_direct_board":
        node = soup.select_one("[data-test='company-name'], .media-heading, .t4")
        if node:
            return clean_text(node.get_text(" "))
    if source == "jobijoba":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.upper() in {"CDI", "CDD"} and index + 1 < len(lines):
                return clean_text(lines[index + 1])
    return _company_from_domain(url)


def _description_from_page(soup: BeautifulSoup) -> str | None:
    return first_text(
        soup, ("[itemprop='description']", ".description", "#description", "main", "article")
    ) or meta_content(soup, "description", "og:description")


def _employment_type(value: object) -> str | None:
    if isinstance(value, list):
        return ", ".join(filter(None, [clean_text(item) for item in value])) or None
    return clean_text(value)


def _infer_contract(text: str | None) -> str | None:
    normalized = (clean_text(text) or "").lower()
    found = []
    for label in ("CDI", "CDD"):
        if label.lower() in normalized:
            found.append(label)
    return ", ".join(found) or None


def _infer_location(text: str | None, config: SearchConfig | None = None) -> str | None:
    if config is None:
        config = SearchConfig()
    normalized = (clean_text(text) or "").lower()
    for term in config.aggregators.location_terms:
        if term in normalized:
            return term.title()
    return None


def _has_regional_signal(location: str | None, description: str | None, config: SearchConfig | None = None) -> bool:
    if config is None:
        config = SearchConfig()
    return location_is_in_scope(f"{location or ''} {description or ''}", config.location_scope)


def _infer_date(text: str | None) -> str | None:
    cleaned = clean_text(text) or ""
    for marker in ("Actualisé le", "Publiée le", "Publié le"):
        if marker in cleaned:
            match = FRENCH_DATE_PHRASE_RE.search(cleaned.split(marker, 1)[1])
            return clean_text(match.group(0)) if match else None
    return None


def _infer_salary(text: str | None) -> str | None:
    cleaned = clean_text(text) or ""
    if "€" not in cleaned and "Euros" not in cleaned:
        return None
    for marker in ("Salaire :", "Salaire"):
        if marker in cleaned:
            return cleaned.split(marker, 1)[1][:80].strip()
    return None


def _company_from_domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return host.title() if host else None


def _source_id(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or url


def _offer_source_id(source: str, url: str, direct_url: str | None) -> str:
    if source == "jobijoba":
        direct_key = external_url_key(direct_url, blocked_host="jobijoba.com")
        if direct_key:
            return direct_key
    return _source_id(url)


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else None
