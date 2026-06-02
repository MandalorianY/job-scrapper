from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import quote_plus, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from scrapper.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.source_rules import get_source_rules
from scrapper.scrapers.common import apply_url, first_text, job_location, json_ld_jobposting, meta_content, org_name
from scrapper.scrapers.web_search import SearchResult, _search
from scrapper.storage import JobStore
from scrapper.text import clean_text, contains_any_terms, has_excluded_contract_signal


SITE_SEARCH_DIRECT_TEMPLATES = {
    "cadremploi": (
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=lyon",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=grenoble",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=chambery",
        "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=annecy",
    ),
    "isarta": (
        "https://isarta.fr/cgi-bin/emplois/jobs?query={query}",
    ),
}
SITE_SEARCH_DIRECT_PREFIXES = {
    "cadremploi": ("/emploi/detail_offre",),
    "isarta": ("/?job=",),
}
EMPLOI_COLLECTIVITES_SITEMAP = "https://www.emploi-collectivites.fr/XML/SiteMap_Offres0.xml"
SITEMAP_URL_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE)


async def scrape_aggregators(config: SearchConfig, store: JobStore) -> dict[str, int]:
    if not config.is_source_enabled(config.aggregators.source):
        return {}

    counts: dict[str, int] = {}
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
        for spec in config.aggregators.direct_specs:
            if not config.is_source_enabled(spec.source):
                continue
            counts[spec.source] = await _scrape_direct_spec(client, spec, config, store)
        site_counts = await _scrape_site_search(client, config, store)
        counts.update(site_counts)
    return counts


async def _scrape_direct_spec(
    client: httpx.AsyncClient,
    spec: DirectAggregatorConfig,
    config: SearchConfig,
    store: JobStore,
) -> int:
    written = 0
    seen_urls: set[str] = set()
    for query in config.queries:
        store.start_search(spec.source, query, config.location_label)
        seen_count = 0
        try:
            for search_url in _search_urls(spec, query, config):
                for detail_url in await _detail_links(client, spec, search_url):
                    if len(seen_urls) >= config.web_search_max_detail_pages:
                        break
                    if detail_url in seen_urls:
                        continue
                    seen_urls.add(detail_url)
                    seen_count += 1
                    if not config.refresh_known_details and store.source_url_seen(spec.source, detail_url):
                        continue
                    offer = await _extract_detail_offer(client, detail_url, spec.source, query, config)
                    if not _offer_is_usable(offer, config):
                        continue
                    store.upsert_offer(offer)
                    written += 1
            store.finish_search(spec.source, query, config.location_label, seen_count)
        except Exception as exc:  # noqa: BLE001 - keep other sites running.
            store.finish_search(spec.source, query, config.location_label, seen_count, error=str(exc))
    return written


async def _scrape_site_search(
    client: httpx.AsyncClient,
    config: SearchConfig,
    store: JobStore,
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
                    results = await _site_search_results(client, spec, query, search_query, config)
                    for result in results:
                        seen += 1
                        if not _site_result_matches_source(spec, result.url):
                            continue
                        if not config.refresh_known_details and store.source_url_seen(spec.source, result.url):
                            continue
                        offer = await _extract_result_offer(
                            client,
                            result,
                            spec.source,
                            search_query,
                            search_location,
                            config,
                        )
                        if not _offer_is_usable(offer, config):
                            continue
                        store.upsert_offer(offer)
                        counts[spec.source] += 1
                    store.finish_search(spec.source, search_query, search_location, seen)
                except Exception as exc:  # noqa: BLE001
                    store.finish_search(spec.source, search_query, search_location, seen, error=str(exc))
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
        return await _direct_site_search_results(client, spec.source, query, config)
    return await _search(client, search_query, min(config.web_search_max_results_per_query, 6), config)


async def _direct_site_search_results(
    client: httpx.AsyncClient,
    source: str,
    query: str,
    config: SearchConfig,
) -> list[SearchResult]:
    results: list[SearchResult] = []
    prefixes = SITE_SEARCH_DIRECT_PREFIXES[source]
    for template in SITE_SEARCH_DIRECT_TEMPLATES[source]:
        search_url = template.format(query=quote_plus(query))
        response = await client.get(search_url)
        if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
            continue
        soup = BeautifulSoup(response.text, "lxml")
        for anchor in soup.select("a[href]"):
            href = anchor.get("href") or ""
            path = urlparse(href).path or urlparse(urljoin(str(response.url), href)).path
            if not any(href.startswith(prefix) or path.startswith(prefix) for prefix in prefixes):
                continue
            url = urljoin(str(response.url), href.split("#", 1)[0])
            if any(existing.url == url for existing in results):
                continue
            title = clean_text(anchor.get_text(" "))
            if title and not contains_any_terms((title,), config.include_terms):
                continue
            results.append(SearchResult(url=url, title=title, snippet=None))
            if len(results) >= config.web_search_max_detail_pages:
                return results
    return results


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


async def _detail_links(client: httpx.AsyncClient, spec: DirectAggregatorConfig, search_url: str) -> list[str]:
    response = await client.get(search_url)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    urls: list[str] = []
    for anchor in soup.select("a[href]"):
        href = anchor.get("href") or ""
        if not any(href.startswith(prefix) or urlparse(href).path.startswith(prefix) for prefix in spec.link_prefixes):
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
) -> JobOffer | None:
    offer = await _extract_detail_offer(client, result.url, source, query, config)
    if offer:
        offer.search_location = search_location
        return offer
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
        scraped_at=datetime.now(timezone.utc).isoformat(),
        raw={"search_result": result.__dict__},
    )


async def _extract_detail_offer(
    client: httpx.AsyncClient,
    url: str,
    source: str,
    query: str,
    config: SearchConfig,
) -> JobOffer | None:
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
        return None

    soup = BeautifulSoup(response.text, "lxml")
    job = json_ld_jobposting(soup) or {}
    text = soup.get_text("\n", strip=True)
    source_rules = get_source_rules(source)
    title = source_rules.normalize_title(
        clean_text(job.get("title"))
        or _clean_title(first_text(soup, ("h1",)))
        or meta_content(soup, "og:title", "twitter:title")
    )
    company = source_rules.extract_company(soup, url) or org_name(job.get("hiringOrganization")) or _company_from_page(source, soup, text, url)
    location = source_rules.extract_location(soup, url) or job_location(job.get("jobLocation")) or _infer_location(text, config)
    description = clean_text(job.get("description")) or source_rules.extract_description(soup, url) or _description_from_page(soup)
    employment_type = _employment_type(job.get("employmentType")) or source_rules.extract_employment_type(soup, url) or _infer_contract(text)

    return JobOffer(
        source=source,
        source_id=_source_id(url),
        source_url=url,
        direct_url=apply_url(soup, url) or url,
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
        scraped_at=datetime.now(timezone.utc).isoformat(),
        raw={"json_ld": job, "url": url},
    )


def _offer_is_usable(offer: JobOffer | None, config: SearchConfig) -> bool:
    if not offer or not offer.title:
        return False
    if has_excluded_contract_signal(offer.title, offer.employment_type, offer.description, config.exclude_terms):
        return False
    if not contains_any_terms((offer.title,), config.include_terms):
        return False
    if not _has_regional_signal(offer.location, offer.description, config):
        return False
    return True


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
            return cleaned.split(marker, 1)[1][:24].strip()
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


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else None
