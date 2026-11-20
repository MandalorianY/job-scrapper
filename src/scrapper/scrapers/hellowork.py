from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlencode

import structlog
from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.scrapers.common import (
    absolutize,
    apply_url,
    custom_search_cutoff,
    first_text,
    job_location,
    json_ld_jobposting,
    org_name,
    posted_date_is_before_cutoff,
)
from scrapper.storage import JobStore
from scrapper.text import clean_text, has_excluded_contract_signal

log = structlog.get_logger(__name__)


async def scrape_hellowork(config: SearchConfig, store: JobStore) -> int:
    if not config.is_source_enabled(config.hellowork.source):
        return 0

    discovered = set[str]()
    detail_enqueued = 0
    written = 0
    log.info(
        "custom_source_started",
        source=config.hellowork.source,
        queries=list(config.queries),
        locations=list(config.search_locations),
        max_pages=config.custom_max_pages,
        max_detail_pages=config.custom_max_detail_pages,
    )

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=max(
            1,
            len(config.queries) * len(config.search_locations) * config.custom_max_pages
            + config.custom_max_detail_pages,
        ),
        max_request_retries=2,
        request_handler_timeout=timedelta(seconds=30),
        navigation_timeout=timedelta(seconds=30),
        concurrency_settings=ConcurrencySettings(
            min_concurrency=config.crawlee_min_concurrency,
            desired_concurrency=config.crawlee_desired_concurrency,
            max_concurrency=config.crawlee_max_concurrency,
            max_tasks_per_minute=max(1, int(60 / config.request_delay_secs)),
        ),
    )

    @crawler.router.handler("LIST")
    async def list_handler(context: BeautifulSoupCrawlingContext) -> None:
        nonlocal detail_enqueued
        request_query = context.request.user_data.get("query")
        request_location = context.request.user_data.get("location")
        page = int(context.request.user_data.get("page") or 1)
        cutoff = custom_search_cutoff(
            store,
            config.hellowork.source,
            request_query,
            request_location,
            config,
        )
        links = []
        for anchor in context.soup.select('a[href*="/fr-fr/emplois/"][href$=".html"]'):
            if detail_enqueued >= config.custom_max_detail_pages:
                break
            title = first_text(anchor, ("h3", "h2")) or clean_text(anchor.get_text(" "))
            if not config.title_is_allowed(title) or has_excluded_contract_signal(title, None, None, config.exclude_terms):
                continue
            listing_date = _listing_date(anchor)
            if posted_date_is_before_cutoff(listing_date, cutoff):
                continue
            url = absolutize(config.hellowork.base_url, anchor.get("href"))
            if not url or url in discovered:
                continue
            discovered.add(url)
            if not config.refresh_known_details and store.source_url_seen(config.hellowork.source, url):
                continue
            detail_enqueued += 1
            links.append(
                Request.from_url(
                    url,
                    label="DETAIL",
                    headers={"user-agent": config.user_agent, **config.extra_headers},
                    user_data={"query": request_query, "location": request_location, "cutoff": cutoff.isoformat()},
                )
            )
        if page < config.custom_max_pages:
            next_page = _search_request(config, request_query, request_location, page + 1)
            if next_page:
                links.append(next_page)
        if links:
            await context.add_requests(links)

    @crawler.router.handler("DETAIL")
    async def detail_handler(context: BeautifulSoupCrawlingContext) -> None:
        nonlocal written
        soup = context.soup
        job = json_ld_jobposting(soup) or {}
        title = clean_text(job.get("title")) or first_text(soup, ("h1", "[data-testid*='title']"))
        company = org_name(job.get("hiringOrganization")) or first_text(
            soup,
            (
                "[data-cy*='company']",
                "[data-testid*='company']",
                ".tw-typo-m",
                "a[href*='/entreprises/']",
            ),
        )
        location = job_location(job.get("jobLocation")) or first_text(
            soup,
            (
                "[data-cy*='localisation']",
                "[data-testid*='location']",
                "[class*='location']",
            ),
        )
        description = clean_text(job.get("description")) or first_text(
            soup,
            (
                "[data-cy='jobDescription']",
                "[data-testid*='description']",
                "section",
                "main",
            ),
        )
        employment_type = _employment_type(job.get("employmentType")) or first_text(
            soup,
            ("[data-cy*='contract']", "[data-testid*='contract']", "[class*='contract']"),
        )
        date_posted = clean_text(job.get("datePosted"))
        cutoff_text = context.request.user_data.get("cutoff")
        cutoff = datetime.fromisoformat(cutoff_text) if cutoff_text else None
        if cutoff and posted_date_is_before_cutoff(date_posted, cutoff):
            return
        if has_excluded_contract_signal(title, employment_type, description, config.exclude_terms):
            return
        if not config.title_is_allowed(title):
            return
        if config.is_company_excluded(config.hellowork.source, company):
            return
        offer = JobOffer(
            source=config.hellowork.source,
            source_url=context.request.url,
            direct_url=apply_url(soup, context.request.url),
            title=title,
            company=company,
            location=location,
            description=description,
            date_posted=date_posted,
            employment_type=employment_type,
            salary=clean_text(job.get("baseSalary")),
            search_query=context.request.user_data.get("query"),
            search_location=context.request.user_data.get("location"),
            raw={"json_ld": job},
        )
        if not location_is_in_scope(offer.location, config.location_scope):
            return
        if offer.title and offer.company:
            store.upsert_offer(offer)
            written += 1

    requests = []
    for query in config.queries:
        for search_location in config.search_locations:
            store.start_search(config.hellowork.source, query, search_location)
            request = _search_request(config, query, search_location, 1)
            if request:
                requests.append(request)
    await crawler.run(requests)
    per_query_seen = len(discovered)
    for query in config.queries:
        for search_location in config.search_locations:
            store.finish_search(config.hellowork.source, query, search_location, per_query_seen)
    log.info(
        "custom_source_completed",
        source=config.hellowork.source,
        seen=per_query_seen,
        enqueued=detail_enqueued,
        stored=written,
    )
    return written


def _employment_type(value) -> str | None:
    if isinstance(value, list):
        return ", ".join(filter(None, [clean_text(item) for item in value])) or None
    return clean_text(value)


def _search_request(config: SearchConfig, query: str | None, search_location: str | None, page: int) -> Request | None:
    if not query or not search_location:
        return None
    params = {
        "k": query,
        "l": search_location,
        "ray": config.distance_km,
        "tri": "date",
        "p": page,
    }
    return Request.from_url(
        f"{config.hellowork.base_url}/fr-fr/emploi/recherche.html?{urlencode(params)}",
        label="LIST",
        headers={"user-agent": config.user_agent, **config.extra_headers},
        user_data={"query": query, "location": search_location, "page": page},
    )


def _listing_date(anchor) -> str | None:
    for container in [anchor, *anchor.parents]:
        if not getattr(container, "select_one", None):
            continue
        node = container.select_one("time[datetime], [datetime], [class*='date'], [data-testid*='date']")
        if node:
            return clean_text(node.get("datetime")) or clean_text(node.get_text(" "))
        text = clean_text(container.get_text(" "))
        if text and ("il y a" in text.lower() or "publié" in text.lower()):
            return text
        if getattr(container, "name", None) in {"article", "li"}:
            break
    return None
