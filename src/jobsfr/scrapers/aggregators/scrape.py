from __future__ import annotations

import asyncio
from dataclasses import replace
from itertools import batched, islice, product

import httpx
import structlog

from jobsfr.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from jobsfr.config.source_names import AGGREGATORS_SOURCE
from jobsfr.offers.models import JobOffer
from jobsfr.scrapers.aggregators.detail import _extract_detail_offer, _offer_is_candidate
from jobsfr.scrapers.aggregators.discovery import (
    EMPLOI_COLLECTIVITES_SITEMAP,
    SITEMAP_URL_RE,
    _detail_links,
    _emploi_collectivites_sitemap_results,
    _search_urls,
    _site_result_matches_source,
    _site_search_results,
    site_search_templates,
)
from jobsfr.scrapers.async_http import HostThrottle, SourceBlockedError, get_checked_page
from jobsfr.scrapers.common import (
    custom_search_cutoff,
    offer_rejection,
)
from jobsfr.storage import JobStore

log = structlog.get_logger(__package__)


async def scrape_aggregators(config: SearchConfig, store: JobStore) -> dict[str, int]:
    if not config.is_source_enabled(AGGREGATORS_SOURCE):
        return {}

    direct_specs = tuple(spec for spec in config.aggregators.direct_specs if config.is_source_enabled(spec.source))
    site_sources = [
        spec.source for spec in config.aggregators.site_search_sources if config.is_source_enabled(spec.source)
    ]
    log.info(
        "aggregators_scrape_started",
        direct_sources=[spec.source for spec in direct_specs],
        site_search_sources=site_sources,
    )
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}
    limits = httpx.Limits(
        max_connections=config.http_max_connections, max_keepalive_connections=config.http_max_connections
    )
    throttle = HostThrottle(config.http_per_host_concurrency, config.http_request_delay_secs)
    async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True, limits=limits) as client:
        site_task: asyncio.Task[dict[str, int]] | None = None
        async with asyncio.TaskGroup() as group:
            direct_tasks = [
                group.create_task(_scrape_direct_spec(client, spec, config, store, throttle)) for spec in direct_specs
            ]
            # Custom specs can share search keys across the two discovery methods.
            if not any(spec.source in site_sources for spec in direct_specs):
                site_task = group.create_task(_scrape_site_search(client, config, store, throttle))
        counts = {spec.source: task.result() for spec, task in zip(direct_specs, direct_tasks, strict=True)}
        site_counts = (
            await _scrape_site_search(client, config, store, throttle) if site_task is None else site_task.result()
        )
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
    batch_size = min(2, throttle.per_host_concurrency) if throttle else 1
    detail_budget = min(spec.max_links, config.aggregators.max_detail_pages)
    for query in config.queries:
        checked_urls: set[str] = set()
        listing_dates: dict[str, str] = {}
        store.start_search(spec.source, query, config.location_label)
        seen_count = 0
        capped = False
        try:
            cutoff = custom_search_cutoff(store, spec.source, query, config.location_label, config)
            for search_url in _search_urls(spec, query, config):
                links = await _detail_links(
                    client,
                    spec,
                    search_url,
                    throttle,
                    config=config,
                    listing_dates=listing_dates,
                )
                candidates = (
                    url
                    for url in links
                    if url not in checked_urls
                    and (config.refresh_known_details or not store.source_url_seen(spec.source, url))
                )
                for batch in batched(islice(candidates, detail_budget), batch_size):
                    checked_urls.update(batch)
                    seen_count += len(batch)
                    offers = await asyncio.gather(
                        *(
                            _extract_detail_offer(
                                client, url, spec.source, query, config, throttle=throttle, cutoff=cutoff
                            )
                            for url in batch
                        ),
                        return_exceptions=True,
                    )
                    batch_error: BaseException | None = None
                    for url, offer in zip(batch, offers, strict=True):
                        if isinstance(offer, BaseException):
                            if batch_error is None or isinstance(offer, SourceBlockedError):
                                batch_error = offer
                            continue
                        if offer and not offer.date_posted:
                            offer.date_posted = listing_dates.get(url)
                        # Relevance-ordered pages can contain newer offers after an old one.
                        if not _offer_is_candidate(offer, cutoff):
                            continue
                        written += store.upsert_offer(
                            offer, rejection=offer_rejection(offer, config, allow_description_location=True)
                        )
                    if batch_error is not None:
                        raise batch_error
                capped = capped or next(candidates, None) is not None
            error = "Detail page limit reached; watermark kept." if capped else None
            store.finish_search(spec.source, query, config.location_label, seen_count, error=error)
            log.info(
                "aggregator_direct_search_finished",
                source=spec.source,
                query=query,
                location=config.location_label,
                seen=seen_count,
                stored=written,
                error=error,
            )
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            store.finish_search(spec.source, query, config.location_label, seen_count, error=error)
            if isinstance(exc, SourceBlockedError):
                break
            log.exception(
                "aggregator_direct_search_failed",
                source=spec.source,
                query=query,
                location=config.location_label,
                seen=seen_count,
                error=error,
            )
    return written


async def _scrape_site_search(
    client: httpx.AsyncClient,
    config: SearchConfig,
    store: JobStore,
    throttle: HostThrottle | None = None,
) -> dict[str, int]:
    sources: dict[str, list[SiteSearchConfig]] = {}
    for spec in config.aggregators.site_search_sources:
        if config.is_source_enabled(spec.source):
            sources.setdefault(spec.source, []).append(spec)
    # Keep specs for the same source together so their search keys cannot overlap.
    async with asyncio.TaskGroup() as group:
        tasks = {
            source: group.create_task(_scrape_site_specs(client, specs, config, store, throttle))
            for source, specs in sources.items()
        }
    return {source: task.result() for source, task in tasks.items()}


async def _scrape_site_specs(
    client: httpx.AsyncClient,
    specs: list[SiteSearchConfig],
    config: SearchConfig,
    store: JobStore,
    throttle: HostThrottle | None,
) -> int:
    written = 0
    sitemap_urls: list[str] | None = None
    for spec in specs:
        parsed_details: dict[str, JobOffer] = {}
        for query, search_location in product(config.queries, config.search_locations):
            search_query = spec.query_template.format(query=query, location=search_location)
            store.start_search(spec.source, search_query, search_location)
            seen = 0
            try:
                cutoff = custom_search_cutoff(store, spec.source, search_query, search_location, config)
                if spec.source == "emploi_collectivites":
                    if sitemap_urls is None:
                        response = await get_checked_page(client, EMPLOI_COLLECTIVITES_SITEMAP, throttle)
                        response.raise_for_status()
                        sitemap_urls = SITEMAP_URL_RE.findall(response.text)
                    urls = _emploi_collectivites_sitemap_results(sitemap_urls, query, config)
                else:
                    templates = site_search_templates(spec, search_location, config)
                    search_spec = spec.model_copy(update={"search_url_templates": templates})
                    urls = await _site_search_results(client, search_spec, query, config, throttle)
                candidates: dict[str, None] = {}
                for url in urls:
                    seen += 1
                    if not _site_result_matches_source(spec, url):
                        continue
                    if not config.refresh_known_details and store.source_url_seen(spec.source, url):
                        continue
                    candidates[url] = None
                unfetched = [url for url in candidates if url not in parsed_details]
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
                        for url in unfetched
                    ),
                    return_exceptions=True,
                )
                fetched = dict(zip(unfetched, offers, strict=True))
                error: BaseException | None = None
                for url in candidates:
                    offer = parsed_details[url] if url in parsed_details else fetched[url]
                    if isinstance(offer, BaseException):
                        if error is None or isinstance(offer, SourceBlockedError):
                            error = offer
                        continue
                    if offer is not None and offer.title:
                        parsed_details[url] = offer
                    if not _offer_is_candidate(offer, cutoff):
                        continue
                    written += store.upsert_offer(
                        replace(offer, search_query=search_query, search_location=search_location),
                        rejection=offer_rejection(offer, config, allow_description_location=True),
                    )
                if error is not None:
                    raise error
                store.finish_search(spec.source, search_query, search_location, seen)
                log.info(
                    "aggregator_site_search_finished",
                    source=spec.source,
                    query=search_query,
                    location=search_location,
                    seen=seen,
                    candidates=len(candidates),
                    stored=written,
                )
            except Exception as exc:
                error_message = str(exc) or type(exc).__name__
                store.finish_search(spec.source, search_query, search_location, seen, error=error_message)
                if isinstance(exc, SourceBlockedError):
                    break
                log.exception(
                    "aggregator_site_search_failed",
                    source=spec.source,
                    query=search_query,
                    location=search_location,
                    seen=seen,
                    error=error_message,
                )
    return written
