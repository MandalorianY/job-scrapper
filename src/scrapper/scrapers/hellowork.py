from __future__ import annotations

from datetime import timedelta
from urllib.parse import urlencode

from crawlee import ConcurrencySettings, Request
from crawlee.crawlers import BeautifulSoupCrawler, BeautifulSoupCrawlingContext

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.scrapers.common import absolutize, apply_url, first_text, job_location, json_ld_jobposting, org_name
from scrapper.storage import JobStore
from scrapper.text import clean_text, contains_any_terms, has_excluded_contract_signal


async def scrape_hellowork(config: SearchConfig, store: JobStore) -> int:
    if not config.is_source_enabled(config.hellowork.source):
        return 0

    discovered = set[str]()
    detail_enqueued = 0
    written = 0

    crawler = BeautifulSoupCrawler(
        max_requests_per_crawl=max(1, len(config.queries) * config.custom_max_pages + config.custom_max_detail_pages),
        max_request_retries=2,
        request_handler_timeout=timedelta(seconds=30),
        navigation_timeout=timedelta(seconds=30),
        concurrency_settings=ConcurrencySettings(
            min_concurrency=1,
            desired_concurrency=1,
            max_concurrency=2,
            max_tasks_per_minute=max(1, int(60 / config.request_delay_secs)),
        ),
    )

    @crawler.router.handler("LIST")
    async def list_handler(context: BeautifulSoupCrawlingContext) -> None:
        nonlocal detail_enqueued
        request_query = context.request.user_data.get("query")
        links = []
        for anchor in context.soup.select('a[href*="/fr-fr/emplois/"][href$=".html"]'):
            if detail_enqueued >= config.custom_max_detail_pages:
                break
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
                    user_data={"query": request_query, "location": context.request.user_data.get("location")},
                )
            )
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
        if has_excluded_contract_signal(title, employment_type, description, config.exclude_terms):
            return
        if not contains_any_terms((title,), config.include_terms):
            return
        offer = JobOffer(
            source=config.hellowork.source,
            source_url=context.request.url,
            direct_url=apply_url(soup, context.request.url),
            title=title,
            company=company,
            location=location,
            description=description,
            date_posted=clean_text(job.get("datePosted")),
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
            for page in range(1, config.custom_max_pages + 1):
                params = {
                    "k": query,
                    "l": search_location,
                    "ray": config.distance_km,
                    "tri": "date",
                    "p": page,
                }
                requests.append(
                    Request.from_url(
                        f"{config.hellowork.base_url}/fr-fr/emploi/recherche.html?{urlencode(params)}",
                        label="LIST",
                        headers={"user-agent": config.user_agent, **config.extra_headers},
                        user_data={"query": query, "location": search_location},
                    )
                )
    await crawler.run(requests)
    per_query_seen = len(discovered)
    for query in config.queries:
        for search_location in config.search_locations:
            store.finish_search(config.hellowork.source, query, search_location, per_query_seen)
    return written


def _employment_type(value) -> str | None:
    if isinstance(value, list):
        return ", ".join(filter(None, [clean_text(item) for item in value])) or None
    return clean_text(value)
