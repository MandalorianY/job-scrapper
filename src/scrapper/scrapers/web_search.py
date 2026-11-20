from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, unquote, urljoin, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
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
from scrapper.storage import JobStore
from scrapper.text import clean_text, has_excluded_contract_signal

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str | None
    snippet: str | None


class SearchUnavailableError(RuntimeError):
    """Every usable free discovery path failed, rather than returning no jobs."""


async def scrape_web_search(config: SearchConfig, store: JobStore) -> int:
    if not config.is_source_enabled(config.web_search.source):
        return 0

    written = 0
    seen_urls: set[str] = set()
    details_checked = 0
    log.info(
        "web_search_started",
        source=config.web_search.source,
        queries=list(config.queries),
        locations=list(config.search_locations),
        max_results_per_query=config.web_search_max_results_per_query,
        max_detail_pages=config.web_search_max_detail_pages,
    )
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}
    limits = httpx.Limits(max_connections=config.http_max_connections, max_keepalive_connections=config.http_max_connections)
    throttle = HostThrottle(config.http_per_host_concurrency, config.http_request_delay_secs)
    async with httpx.AsyncClient(headers=headers, timeout=25, follow_redirects=True, limits=limits) as client:
        for query in config.queries:
            for search_location in config.search_locations:
                search_query = _search_query(query, search_location, config)
                store.start_search(config.web_search.source, search_query, search_location)
                seen = 0
                try:
                    cutoff = custom_search_cutoff(store, config.web_search.source, search_query, search_location, config)
                    results = await _search(client, search_query, config.web_search_max_results_per_query, config)
                    candidates: list[SearchResult] = []
                    for result in results:
                        seen += 1
                        if details_checked >= config.web_search_max_detail_pages:
                            break
                        if (
                            result.url in seen_urls
                            or _is_excluded_domain(result.url, config)
                            or _is_info_page(result.url, config)
                            or store.source_url_seen(config.web_search.source, result.url)
                        ):
                            continue
                        seen_urls.add(result.url)
                        details_checked += 1
                        candidates.append(result)
                    offers = await asyncio.gather(
                        *(
                            _extract_offer(client, result, search_query, search_location, config, throttle=throttle)
                            for result in candidates
                        )
                    )
                    for offer in offers:
                        if not offer:
                            continue
                        if posted_date_is_before_cutoff(offer.date_posted, cutoff):
                            continue
                        if has_excluded_contract_signal(
                            offer.title,
                            offer.employment_type,
                            offer.description,
                            config.exclude_terms,
                        ):
                            continue
                        if not config.title_is_allowed(offer.title):
                            continue
                        if config.is_company_excluded(offer.source, offer.company):
                            continue
                        store.upsert_offer(offer)
                        written += 1
                    store.finish_search(config.web_search.source, search_query, search_location, seen)
                    log.info(
                        "web_search_finished",
                        source=config.web_search.source,
                        query=search_query,
                        location=search_location,
                        seen=seen,
                        candidates=len(candidates),
                        stored=written,
                    )
                except Exception as exc:
                    store.finish_search(config.web_search.source, search_query, search_location, seen, error=str(exc))
                    log.exception(
                        "web_search_failed",
                        source=config.web_search.source,
                        query=search_query,
                        location=search_location,
                        seen=seen,
                    )
    log.info("web_search_completed", source=config.web_search.source, stored=written)
    return written


async def _search(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig | None = None) -> list[SearchResult]:
    if config is None:
        config = SearchConfig()
    results: list[SearchResult] = []
    failures: list[str] = []
    for search in (_search_google, _search_duckduckgo, _search_bing):
        try:
            results.extend(await search(client, query, limit, config))
        except (httpx.HTTPError, SearchUnavailableError) as exc:
            reason = f"HTTP {exc.response.status_code}" if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
            failures.append(f"{search.__name__.removeprefix('_search_')}: {reason}")
            continue
        if len(_dedupe_results(results)) >= limit:
            break
    if len(failures) == 3:
        raise SearchUnavailableError("All free search providers failed: " + "; ".join(failures))
    return _dedupe_results(results)[:limit]


async def _search_duckduckgo(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    response = await client.get(config.web_search.duckduckgo_html_url, params={"q": query})
    response.raise_for_status()
    if response.status_code == 202 or "challenge-form" in response.text:
        raise SearchUnavailableError("DuckDuckGo returned a challenge page")
    soup = BeautifulSoup(response.text, "lxml")
    results: list[SearchResult] = []
    for result in soup.select(".result"):
        anchor = result.select_one("a.result__a")
        if not anchor:
            continue
        url = _unwrap_ddg(anchor.get("href"))
        if not url or any(existing.url == url for existing in results):
            continue
        snippet_node = result.select_one(".result__snippet")
        results.append(
            SearchResult(
                url=url,
                title=clean_text(anchor.get_text(" ")),
                snippet=clean_text(snippet_node.get_text(" ")) if snippet_node else None,
            )
        )
        if len(results) >= limit:
            break
    return results


async def _search_google(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    response = await client.get(
        "https://www.google.com/search",
        params={
            "q": query,
            "num": limit,
            "hl": config.search_engines.hl,
            "gl": config.search_engines.gl,
            "pws": "0",
        },
    )
    response.raise_for_status()
    if "google.com/sorry" in str(response.url):
        raise SearchUnavailableError("Google returned a challenge page")
    soup = BeautifulSoup(response.text, "lxml")
    results: list[SearchResult] = []
    for heading in soup.select("h3"):
        anchor = heading.find_parent("a")
        url = _unwrap_google(anchor.get("href") if anchor else None)
        if not url or any(existing.url == url for existing in results):
            continue
        snippet = None
        container = heading.find_parent("div")
        if container:
            text = clean_text(container.get_text(" "))
            title = clean_text(heading.get_text(" "))
            snippet = text.replace(title or "", "", 1).strip() if text and title else text
        results.append(SearchResult(url=url, title=clean_text(heading.get_text(" ")), snippet=snippet))
        if len(results) >= limit:
            break
    return results


async def _search_bing(client: httpx.AsyncClient, query: str, limit: int, config: SearchConfig) -> list[SearchResult]:
    response = await client.get(
        "https://www.bing.com/search",
        params={
            "q": query,
            "count": limit,
            "setlang": config.http.accept_language.split(",", 1)[0],
            "cc": config.search_engines.gl,
        },
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")
    if "Veuillez résoudre le défi" in soup.get_text(" ", strip=True):
        raise SearchUnavailableError("Bing returned a challenge page")
    results: list[SearchResult] = []
    for item in soup.select("li.b_algo"):
        anchor = item.select_one("h2 a[href]")
        url = _unwrap_bing(anchor.get("href") if anchor else None)
        if not url or any(existing.url == url for existing in results):
            continue
        snippet = item.select_one(".b_caption p, p")
        results.append(
            SearchResult(
                url=url,
                title=clean_text(anchor.get_text(" ")) if anchor else None,
                snippet=clean_text(snippet.get_text(" ")) if snippet else None,
            )
        )
        if len(results) >= limit:
            break
    return results


def _dedupe_results(results: list[SearchResult]) -> list[SearchResult]:
    deduped: list[SearchResult] = []
    for result in results:
        if result.url and not any(existing.url == result.url for existing in deduped):
            deduped.append(result)
    return deduped


async def _extract_offer(
    client: httpx.AsyncClient,
    result: SearchResult,
    search_query: str,
    search_location: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
) -> JobOffer | None:
    url = result.url
    if _is_info_page(url, config):
        return None
    soup = None
    job = None
    try:
        response = await throttle.get(client, url) if throttle else await client.get(url)
        if response.status_code < 400 and "text/html" in response.headers.get("content-type", ""):
            soup = BeautifulSoup(response.text, "lxml")
            job = json_ld_jobposting(soup)
    except httpx.HTTPError:
        soup = None

    if job:
        title = clean_text(job.get("title"))
        company = org_name(job.get("hiringOrganization")) or _company_from_domain(url)
        location = job_location(job.get("jobLocation"))
        description = clean_text(job.get("description")) or meta_content(soup, "description", "og:description")
        employment_type = _employment_type(job.get("employmentType"))
        source_url = clean_text(job.get("url")) or url
    elif soup is not None:
        title = first_text(soup, ("h1", "title")) or meta_content(soup, "og:title", "twitter:title")
        description = meta_content(soup, "description", "og:description") or first_text(soup, ("main", "article"))
        company = _company_from_domain(url)
        location = _infer_location(description, config)
        employment_type = _infer_contract(description)
        source_url = url
    else:
        title = result.title
        description = result.snippet
        company = _company_from_domain(url)
        location = _infer_location(f"{result.title or ''} {result.snippet or ''}", config)
        employment_type = _infer_contract(f"{result.title or ''} {result.snippet or ''}")
        source_url = url

    if not title or not _looks_like_job(title, description, config):
        return None
    if not job:
        # Search snippets and general articles can mention both a city and CDI.
        # Require a listing URL or an explicit invitation to apply as well.
        listing_path = any(part in urlparse(url).path.lower() for part in (
            "/jobs/", "/job/", "/emplois/", "/emploi/", "/offres/", "/offre/", "/careers/",
        ))
        application_signal = any(term in (description or "").casefold() for term in (
            "postulez", "envoyez votre candidature", "déposer votre candidature", "apply for this job",
        ))
        if not listing_path and not application_signal:
            return None
    if not _looks_regional(location, description, config):
        return None

    return JobOffer(
        source=config.web_search.source,
        source_id=url,
        source_url=source_url,
        direct_url=apply_url(soup, url) if soup is not None else source_url,
        title=title,
        company=company,
        location=location,
        description=description,
        date_posted=clean_text(job.get("datePosted")) if job else None,
        employment_type=employment_type,
        salary=clean_text(job.get("baseSalary")) if job else None,
        company_url=_site_root(url),
        search_query=search_query,
        search_location=search_location,
        scraped_at=datetime.now(UTC).isoformat(),
        raw={"json_ld": job, "discovered_url": url} if job else {"discovered_url": url},
    )


def _search_query(query: str, location: str, config: SearchConfig | None = None) -> str:
    if config is None:
        config = SearchConfig()
    return config.web_search.query_template.format(query=query, location=location)


def _unwrap_ddg(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [None])[0]
        return unquote(target) if target else None
    if parsed.scheme in {"http", "https"}:
        return href
    return None


def _unwrap_google(href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("/url?"):
        target = parse_qs(urlparse(href).query).get("q", [None])[0]
        return _valid_result_url(unquote(target)) if target else None
    return _valid_result_url(urljoin("https://www.google.com", href))


def _unwrap_bing(href: str | None) -> str | None:
    if not href:
        return None
    parsed = urlparse(href)
    if parsed.hostname in {"bing.com", "www.bing.com"} and parsed.path == "/ck/a":
        target = parse_qs(parsed.query).get("u", [""])[0]
        if target.startswith("a1"):
            encoded = target[2:]
            try:
                target = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
            except (ValueError, binascii.Error):
                return None
        return _valid_result_url(target)
    return _valid_result_url(href)


def _valid_result_url(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    blocked_hosts = ("google.", "bing.com", "microsoft.com", "support.google.com", "consent.google.com")
    if any(host in parsed.netloc.lower() for host in blocked_hosts):
        return None
    return url


def _is_excluded_domain(url: str, config: SearchConfig | None = None) -> bool:
    if config is None:
        config = SearchConfig()
    host = urlparse(url).netloc.lower()
    return any(domain in host for domain in config.web_search.excluded_domains)


def _is_info_page(url: str, config: SearchConfig | None = None) -> bool:
    if config is None:
        config = SearchConfig()
    path = urlparse(url).path.lower()
    return path.startswith("/wiki/") or any(marker in path for marker in config.web_search.info_path_markers)


def _company_from_domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return host.title() if host else None


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}/"


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


def _looks_like_job(title: str | None, description: str | None, config: SearchConfig | None = None) -> bool:
    if config is None:
        config = SearchConfig()
    blob = f"{title or ''} {description or ''}".lower()
    return any(term in blob for term in config.web_search.job_signal_terms)


def _looks_regional(location: str | None, description: str | None, config: SearchConfig | None = None) -> bool:
    if config is None:
        config = SearchConfig()
    return location_is_in_scope(f"{location or ''} {description or ''}", config.location_scope)
