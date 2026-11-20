from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from scrapper.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
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
from scrapper.scrapers.web_search import SearchResult, _search
from scrapper.source_rules import get_source_rules
from scrapper.source_rules.meteojob import is_placeholder_offer
from scrapper.storage import JobStore
from scrapper.text import clean_text, has_excluded_contract_signal, url_key

log = structlog.get_logger(__name__)

SITE_SEARCH_DIRECT_TEMPLATES = {
    "apec": ("https://www.apec.fr/candidat/recherche-emploi.html/emploi?motsCles={query}",),
    "optioncarriere": ("https://www.optioncarriere.com/emploi-{query}/Toulouse",),
    "cadremploi": (
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=toulouse-31",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=grenoble-38",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=albertville-73",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=annecy-74",
    ),
    "isarta": (
        "https://isarta.fr/cgi-bin/emplois/jobs?query={query}",
    ),
}
SITE_SEARCH_DIRECT_PREFIXES = {
    "apec": ("/candidat/recherche-emploi.html/emploi/detail-offre/",),
    "optioncarriere": ("/jobad/",),
    "cadremploi": ("/emploi/detail_offre",),
    "isarta": ("/?job=",),
}
EMPLOI_COLLECTIVITES_SITEMAP = "https://www.emploi-collectivites.fr/XML/SiteMap_Offres0.xml"
SITEMAP_URL_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE)
FRENCH_MONTH_RE = (
    r"janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|"
    r"septembre|octobre|novembre|décembre|decembre"
)
DATE_VALUE_RE = re.compile(
    rf"""
    (?:
        il\s+y\s+a\s+\d+\s*(?:min|mn|minutes?|h|heures?|j|jours?|d|semaines?|mois)
        | aujourd'hui
        | hier
        | \d{{1,2}}/\d{{1,2}}/\d{{4}}
        | \d{{1,2}}\s+(?:{FRENCH_MONTH_RE})(?:\s+\d{{4}})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


async def scrape_aggregators(config: SearchConfig, store: JobStore) -> dict[str, int]:
    if not config.is_source_enabled(config.aggregators.source):
        return {}

    counts: dict[str, int] = {}
    log.info(
        "aggregators_scrape_started",
        direct_sources=[spec.source for spec in config.aggregators.direct_specs if config.is_source_enabled(spec.source)],
        site_search_sources=[
            spec.source for spec in config.aggregators.site_search_sources if config.is_source_enabled(spec.source)
        ],
    )
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}
    limits = httpx.Limits(max_connections=config.http_max_connections, max_keepalive_connections=config.http_max_connections)
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
    seen_urls: set[str] = set()
    for query in config.queries:
        store.start_search(spec.source, query, config.location_label)
        seen_count = 0
        try:
            cutoff = custom_search_cutoff(store, spec.source, query, config.location_label, config)
            for search_url in _search_urls(spec, query, config):
                for detail_url in await _detail_links(client, spec, search_url, throttle, config=config):
                    if len(seen_urls) >= config.web_search_max_detail_pages:
                        break
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    seen_count += 1
                    if not config.refresh_known_details and store.source_url_seen(spec.source, detail_url):
                        continue
                    offer = await _extract_detail_offer(client, detail_url, spec.source, query, config, throttle=throttle)
                    if offer and posted_date_is_before_cutoff(offer.date_posted, cutoff):
                        # Aggregators often sort by relevance, not publication date.
                        # An old hit does not imply the remaining hits are old.
                        continue
                    if not _offer_is_usable(offer, config):
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
        spec.source: 0
        for spec in config.aggregators.site_search_sources
        if config.is_source_enabled(spec.source)
    }
    for spec in config.aggregators.site_search_sources:
        if not config.is_source_enabled(spec.source):
            continue
        for query in config.queries[:3]:
            for search_location in config.search_locations:
                search_query = spec.query_template.format(query=query, location=search_location)
                store.start_search(spec.source, search_query, search_location)
                seen = 0
                try:
                    cutoff = custom_search_cutoff(store, spec.source, search_query, search_location, config)
                    results = await _site_search_results(client, spec, query, search_query, config)
                    candidates: list[SearchResult] = []
                    for result in results:
                        seen += 1
                        if not _site_result_matches_source(spec, result.url):
                            continue
                        if not config.refresh_known_details and store.source_url_seen(spec.source, result.url):
                            continue
                        candidates.append(result)
                    offers = await asyncio.gather(
                        *(
                            _extract_result_offer(
                                client,
                                result,
                                spec.source,
                                search_query,
                                search_location,
                                config,
                                throttle=throttle,
                            )
                            for result in candidates
                        )
                    )
                    for offer in offers:
                        if offer and posted_date_is_before_cutoff(offer.date_posted, cutoff):
                            continue
                        if not _offer_is_usable(offer, config):
                            continue
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
    search_query: str,
    config: SearchConfig,
) -> list[SearchResult]:
    if spec.source == "emploi_collectivites":
        return await _emploi_collectivites_sitemap_results(client, query, config)
    if spec.source in SITE_SEARCH_DIRECT_TEMPLATES:
        return await _direct_site_search_results(client, spec.source, query, config, spec.search_url_templates or None)
    return await _search(client, search_query, min(config.web_search_max_results_per_query, 6), config)


async def _direct_site_search_results(
    client: httpx.AsyncClient,
    source: str,
    query: str,
    config: SearchConfig,
    search_url_templates: tuple[str, ...] | None = None,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    prefixes = SITE_SEARCH_DIRECT_PREFIXES[source]
    failures: list[str] = []
    successful_pages = 0
    for template in search_url_templates or SITE_SEARCH_DIRECT_TEMPLATES[source]:
        search_url = template.format(query=quote_plus(query))
        try:
            response = await cadremploi.get_page(client, search_url) if source == "cadremploi" else await client.get(search_url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
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
            href = anchor.get("href") or ""
            parsed = urlparse(urljoin(str(response.url), href))
            path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if not any(href.startswith(prefix) or path.startswith(prefix) for prefix in prefixes):
                continue
            url = urljoin(str(response.url), href.split("#", 1)[0])
            if any(existing.url == url for existing in results):
                continue
            title = clean_text(anchor.get_text(" "))
            if not title or not config.title_is_allowed(title):
                continue
            if has_excluded_contract_signal(title, None, None, config.exclude_terms):
                continue
            results.append(SearchResult(url=url, title=title, snippet=None))
    if not successful_pages:
        raise RuntimeError(f"{source} discovery failed: " + "; ".join(dict.fromkeys(failures)))
    if source == "isarta":
        results.sort(key=lambda result: not location_is_in_scope(result.title, config.location_scope))
    return results[:config.web_search_max_detail_pages]


async def _emploi_collectivites_sitemap_results(
    client: httpx.AsyncClient,
    query: str,
    config: SearchConfig,
) -> list[SearchResult]:
    response = await client.get(EMPLOI_COLLECTIVITES_SITEMAP)
    response.raise_for_status()
    results: list[SearchResult] = []
    include_terms = tuple(term.lower().replace(" ", "-") for term in config.include_terms)
    query_terms = tuple(part.lower() for part in query.split() if len(part) > 3)
    regional_terms = tuple(term.lower().replace(" ", "-") for term in config.web_search.location_terms)
    for url in SITEMAP_URL_RE.findall(response.text):
        lowered = url.lower()
        if not any(term in lowered for term in include_terms + query_terms):
            continue
        if not any(term in lowered for term in regional_terms):
            continue
        slug = urlparse(url).path.rstrip("/").split("/")[-1]
        title = slug.rsplit("-", 1)[0].replace("-", " ").title()
        results.append(SearchResult(url=url, title=title, snippet=None))
        if len(results) >= config.web_search_max_detail_pages:
            break
    return results


async def _detail_links(
    client: httpx.AsyncClient,
    spec: DirectAggregatorConfig,
    search_url: str,
    throttle: HostThrottle | None = None,
    config: SearchConfig | None = None,
) -> list[str]:
    response = await throttle.get(client, search_url) if throttle else await client.get(search_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    urls: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if spec.source == "makesense" and not urlparse(urljoin(spec.base_url, href)).path.startswith("/fr/jobs/"):
            continue
        if not any(href.startswith(prefix) or urlparse(href).path.startswith(prefix) for prefix in spec.link_prefixes):
            continue
        if config and spec.source in {"france_travail", "emploi_territorial", "meteojob", "makesense"}:
            title = clean_text(anchor.get_text(" "))
            if not config.title_is_allowed(title) or has_excluded_contract_signal(title, None, None, config.exclude_terms):
                continue
        url = urljoin(spec.base_url, href.split("#", 1)[0])
        if url not in urls:
            urls.append(url)
        if len(urls) >= spec.max_links:
            break
    return urls


async def _extract_result_offer(
    client: httpx.AsyncClient,
    result: SearchResult,
    source: str,
    query: str,
    search_location: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
) -> JobOffer | None:
    offer = await _extract_detail_offer(client, result.url, source, query, config, throttle=throttle)
    if offer:
        offer.search_location = search_location
        return offer
    if not result.snippet:
        # A title-only link cannot verify the contract or role when detail
        # retrieval fails (for example, an Isarta access block).
        return None
    location = _infer_location(f"{result.title or ''} {result.snippet or ''}", config)
    return JobOffer(
        source=source,
        source_id=result.url,
        source_url=result.url,
        direct_url=result.url,
        title=result.title,
        company=_company_from_domain(result.url),
        location=location,
        description=result.snippet,
        search_query=query,
        search_location=search_location,
        scraped_at=datetime.now(UTC).isoformat(),
        raw={"search_result": result.__dict__},
    )


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
    company = source_rules.extract_company(soup, url) or org_name(job.get("hiringOrganization")) or _company_from_page(source, soup, text, url)
    location = source_rules.extract_location(soup, url) or job_location(job.get("jobLocation")) or _infer_location(text, config)
    description = clean_text(job.get("description")) or source_rules.extract_description(soup, url) or _description_from_page(soup)
    employment_type = _employment_type(job.get("employmentType")) or source_rules.extract_employment_type(soup, url) or _infer_contract(text)
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
        date_posted=clean_text(job.get("datePosted")) or source_rules.extract_date(soup, url) or _infer_date(text),
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
        target = _jobijoba_external_target(anchor.get("href"))
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


def _offer_is_usable(offer: JobOffer | None, config: SearchConfig) -> bool:
    if not offer or not offer.title:
        return False
    if _is_meteojob_placeholder_offer(offer):
        return False
    if has_excluded_contract_signal(offer.title, offer.employment_type, offer.description, config.exclude_terms):
        return False
    if not config.title_is_allowed(offer.title):
        return False
    if config.is_company_excluded(getattr(offer, "source", None), getattr(offer, "company", None)):
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
    query_value = query.title().replace(" ", "-") if spec.query_style == "title_slug" else query
    encoded_query = quote_plus(query_value)
    departments = config.aggregators.direct_locations if spec.use_direct_locations else (None,)
    urls = []
    for department in departments:
        for template in spec.search_url_templates:
            urls.append(template.format(query=encoded_query, department=department or ""))
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
    return (
        first_text(soup, ("[itemprop='description']", ".description", "#description", "main", "article"))
        or meta_content(soup, "description", "og:description")
    )


def _employment_type(value) -> str | None:
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
    for term in config.web_search.location_terms:
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
            match = DATE_VALUE_RE.search(cleaned.split(marker, 1)[1])
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
        direct_key = _external_url_key(direct_url, blocked_host="jobijoba.com")
        if direct_key:
            return direct_key
    return _source_id(url)


def _external_url_key(url: str | None, blocked_host: str) -> str | None:
    parsed = urlparse(url or "")
    if not parsed.netloc or blocked_host in parsed.netloc.lower():
        return None
    return url_key(url)


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else None
