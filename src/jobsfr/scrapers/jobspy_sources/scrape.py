from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from jobspy import scrape_jobs

from jobsfr.config import SearchConfig
from jobsfr.config.source_names import JOBSPY_SITES
from jobsfr.offers.models import JobOffer
from jobsfr.scrapers.jobspy_sources.hellowork import patch_hellowork_scraper
from jobsfr.scrapers.jobspy_sources.linkedin import patch_linkedin_scraper
from jobsfr.scrapers.jobspy_sources.offers import offer_is_usable, row_to_offer, string_or_none
from jobsfr.storage import JobStore

INDEED_EXPIRED_MESSAGE = "cette offre a expiré sur indeed"
DETAIL_FETCH_SITES = {"glassdoor", "hellowork"}
KM_PER_MILE = 1.609344
ProgressReporter = Callable[[str], None]
log = structlog.get_logger(__package__)


@dataclass(frozen=True)
class JobSpySearchPlan:
    sites: tuple[str, ...]
    query: str
    location: str
    hours_old: int
    results_wanted: int

    @property
    def label(self) -> str:
        return ",".join(self.sites)


def _hours_old(store: JobStore, source: str, query: str, location: str, config: SearchConfig) -> int:
    if config.jobspy_hours_old_override:
        return config.jobspy_hours_old_override
    last_success = store.get_last_success(source, query, location)
    if not last_success:
        return config.default_hours_old
    now = datetime.now(UTC)
    elapsed_hours = max(1, int((now - last_success).total_seconds() // 3600))
    return elapsed_hours + config.overlap_hours


def scrape_jobspy_sources(
    config: SearchConfig,
    store: JobStore,
    progress: ProgressReporter | None = None,
) -> dict[str, int]:
    with httpx.Client(
        headers={"user-agent": config.user_agent, "accept-language": config.http.accept_language},
        follow_redirects=True,
        timeout=15,
    ) as client:
        return _run_jobspy_searches(config, store, progress, partial(_is_expired_indeed_offer, client, {}))


def _run_jobspy_searches(
    config: SearchConfig,
    store: JobStore,
    progress: ProgressReporter | None,
    is_expired_indeed: Callable[[JobOffer], bool],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    enabled_sites = [site for site in JOBSPY_SITES if config.is_source_enabled(site)]
    for site in enabled_sites:
        counts[site] = 0
    plans = _jobspy_search_plans(enabled_sites, config, store)
    total_searches = len(plans)
    log.info("jobspy_scrape_started", sites=enabled_sites, searches=total_searches)
    for search_index, plan in enumerate(plans, start=1):
        for site in plan.sites:
            store.start_search(site, plan.query, plan.location)
        try:
            plan_counts = _run_jobspy_plan(
                plan, config, store, progress, search_index, total_searches, is_expired_indeed
            )
            for site, count in plan_counts.items():
                counts[site] += count
        except Exception as exc:
            log.exception(
                "jobspy_plan_failed",
                sites=list(plan.sites),
                query=plan.query,
                location=plan.location,
                search_index=search_index,
                total_searches=total_searches,
            )
            if len(plan.sites) > 1:
                for site in plan.sites:
                    store.finish_search(site, plan.query, plan.location, 0, error=str(exc))
                if progress:
                    progress(
                        f"JobSpy batch failed [{search_index}/{total_searches}]: "
                        f"{plan.label} | {plan.query} | {plan.location} "
                        f"(error={_short_error(exc)}); retrying individually"
                    )
                fallback_plans = [plan_for_site for plan_for_site in _solo_plans(plan)]
                for fallback_index, fallback_plan in enumerate(fallback_plans, start=1):
                    store.start_search(fallback_plan.sites[0], fallback_plan.query, fallback_plan.location)
                    try:
                        plan_counts = _run_jobspy_plan(
                            fallback_plan,
                            config,
                            store,
                            progress,
                            fallback_index,
                            len(fallback_plans),
                            is_expired_indeed,
                        )
                        for site, count in plan_counts.items():
                            counts[site] += count
                    except Exception as fallback_exc:
                        log.exception(
                            "jobspy_fallback_plan_failed",
                            sites=list(fallback_plan.sites),
                            query=fallback_plan.query,
                            location=fallback_plan.location,
                            search_index=fallback_index,
                            total_searches=len(fallback_plans),
                        )
                        _finish_failed_plan(
                            fallback_plan, store, progress, fallback_index, len(fallback_plans), fallback_exc
                        )
                continue
            _finish_failed_plan(plan, store, progress, search_index, total_searches, exc)
    log.info("jobspy_scrape_completed", counts=counts)
    return counts


def _jobspy_search_plans(
    enabled_sites: list[str],
    config: SearchConfig,
    store: JobStore,
) -> list[JobSpySearchPlan]:
    plans: list[JobSpySearchPlan] = []
    for query in config.queries:
        for configured_location in config.search_locations:
            grouped_sites: defaultdict[tuple[str, int, int], list[str]] = defaultdict(list)
            for site in enabled_sites:
                search_location = _site_location(site, configured_location, config)
                hours_old = _hours_old(store, site, query, search_location, config)
                results_wanted = _results_wanted(site, config)
                if config.jobspy_batch_sites and site not in DETAIL_FETCH_SITES:
                    grouped_sites[(search_location, hours_old, results_wanted)].append(site)
                else:
                    plans.append(
                        JobSpySearchPlan(
                            sites=(site,),
                            query=query,
                            location=search_location,
                            hours_old=hours_old,
                            results_wanted=results_wanted,
                        )
                    )
            for (search_location, hours_old, results_wanted), sites in grouped_sites.items():
                plans.append(
                    JobSpySearchPlan(
                        sites=tuple(sites),
                        query=query,
                        location=search_location,
                        hours_old=hours_old,
                        results_wanted=results_wanted,
                    )
                )
    return plans


def _solo_plans(plan: JobSpySearchPlan) -> list[JobSpySearchPlan]:
    return [replace(plan, sites=(site,)) for site in plan.sites]


def _run_jobspy_plan(
    plan: JobSpySearchPlan,
    config: SearchConfig,
    store: JobStore,
    progress: ProgressReporter | None,
    search_index: int,
    total_searches: int,
    is_expired_indeed: Callable[[JobOffer], bool],
) -> dict[str, int]:
    if "linkedin" in plan.sites:
        patch_linkedin_scraper()
    if "hellowork" in plan.sites:
        patch_hellowork_scraper()
    log.info(
        "jobspy_plan_started",
        sites=list(plan.sites),
        query=plan.query,
        location=plan.location,
        hours_old=plan.hours_old,
        results_wanted=plan.results_wanted,
        search_index=search_index,
        total_searches=total_searches,
    )
    if progress:
        progress(
            f"JobSpy start [{search_index}/{total_searches}]: "
            f"{plan.label} | {plan.query} | {plan.location} "
            f"(hours_old={plan.hours_old}, results_wanted={plan.results_wanted})"
        )
    distance = config.distance_km
    if plan.sites == ("hellowork",):
        # JobSpy's HelloWork scraper converts its integer distance from miles.
        distance = round(distance / KM_PER_MILE)
    with _quiet_known_noisy_loggers_for_sites(plan.sites):
        jobs = scrape_jobs(
            site_name=plan.sites[0] if len(plan.sites) == 1 else list(plan.sites),
            search_term=plan.query,
            google_search_term=_google_search_term_for_sites(plan.sites, plan.query, plan.location, config),
            location=plan.location,
            distance=distance,
            results_wanted=plan.results_wanted,
            country_indeed=config.country,
            hours_old=plan.hours_old,
            description_format="markdown",
            verbose=_jobspy_verbose_for_sites(plan.sites),
            fetch_description=len(plan.sites) == 1 and plan.sites[0] in DETAIL_FETCH_SITES,
        )

    if not jobs.index.empty:
        required_columns = {"job_url", "site"} if len(plan.sites) > 1 else {"job_url"}
        missing_columns = required_columns.difference(jobs.columns)
        if missing_columns:
            raise ValueError(f"JobSpy response is missing required columns ({', '.join(sorted(missing_columns))})")

    seen_by_site = dict.fromkeys(plan.sites, 0)
    stored_by_site = dict.fromkeys(plan.sites, 0)
    for row in jobs.to_dict(orient="records"):
        site = _row_site(row, plan)
        if site not in seen_by_site:
            continue
        seen_by_site[site] += 1
        offer = row_to_offer(row, site, plan.query, plan.location)
        if not offer_is_usable(offer, row, config) or is_expired_indeed(offer):
            continue
        store.upsert_offer(offer)
        stored_by_site[site] += 1

    for site in plan.sites:
        _finish_successful_site(
            site,
            plan.query,
            plan.location,
            seen_by_site[site],
            stored_by_site[site],
            store,
            progress,
            search_index,
            total_searches,
            source_error=jobs.attrs.get("errors", {}).get(site),
        )
    return stored_by_site


def _finish_successful_site(
    site: str,
    query: str,
    location: str,
    seen: int,
    stored: int,
    store: JobStore,
    progress: ProgressReporter | None,
    search_index: int,
    total_searches: int,
    source_error: str | None = None,
) -> None:
    error = source_error or _zero_rows_error(site, seen)
    if error:
        store.finish_search(site, query, location, seen, error=error)
        log.warning(
            "jobspy_search_finished_with_error",
            source=site,
            query=query,
            location=location,
            seen=seen,
            stored=stored,
            error=error,
        )
        if progress:
            progress(
                f"JobSpy done [{search_index}/{total_searches}]: "
                f"{site} | {query} | {location} "
                f"(seen={seen}, stored={stored}, error={error if source_error else 'zero rows'})"
            )
        return
    store.finish_search(site, query, location, seen)
    log.info(
        "jobspy_search_finished",
        source=site,
        query=query,
        location=location,
        seen=seen,
        stored=stored,
    )
    if progress:
        progress(
            f"JobSpy done [{search_index}/{total_searches}]: "
            f"{site} | {query} | {location} "
            f"(seen={seen}, stored={stored})"
        )


def _finish_failed_plan(
    plan: JobSpySearchPlan,
    store: JobStore,
    progress: ProgressReporter | None,
    search_index: int,
    total_searches: int,
    exc: Exception,
) -> None:
    for site in plan.sites:
        store.finish_search(site, plan.query, plan.location, 0, error=str(exc))
        log.error(
            "jobspy_search_failed",
            source=site,
            query=plan.query,
            location=plan.location,
            seen=0,
            stored=0,
            error=str(exc),
        )
        if progress:
            progress(
                f"JobSpy failed [{search_index}/{total_searches}]: "
                f"{site} | {plan.query} | {plan.location} "
                f"(seen=0, stored=0, error={_short_error(exc)})"
            )


def _zero_rows_error(site: str, seen: int) -> str | None:
    if seen:
        return None
    if site == "google":
        return (
            "JobSpy Google returned 0 rows. Upstream issue #302 reports 'initial cursor not found' "
            "when Google does not return the expected Jobs payload."
        )
    if site == "glassdoor":
        return (
            "JobSpy Glassdoor returned 0 rows after using the city-only location fallback. "
            "Glassdoor may be blocking the Graph API or returning an unsupported payload."
        )
    return None


def _row_site(row: Mapping[str, object], plan: JobSpySearchPlan) -> str:
    if len(plan.sites) == 1:
        return plan.sites[0]
    value = string_or_none(row.get("site"))
    return value.casefold() if value else ""


def _jobspy_verbose(site: str) -> int | None:
    if site == "glassdoor":
        return None
    return 0


def _jobspy_verbose_for_sites(sites: tuple[str, ...]) -> int | None:
    if len(sites) == 1:
        return _jobspy_verbose(sites[0])
    return 0


def _short_error(exc: Exception, limit: int = 180) -> str:
    message = str(exc).replace("\n", " ").strip() or exc.__class__.__name__
    if len(message) <= limit:
        return message
    return f"{message[: limit - 3]}..."


def _results_wanted(site: str, config: SearchConfig) -> int:
    if site == "glassdoor":
        return min(config.jobspy.glassdoor_results_wanted_per_query, config.results_wanted_per_query)
    return config.results_wanted_per_query


def _is_expired_indeed_offer(
    client: httpx.Client,
    checked: dict[tuple[str, str], bool],
    offer: JobOffer,
) -> bool:
    if offer.source != "indeed":
        return False
    for url in dict.fromkeys((offer.source_url, offer.direct_url)):
        if not url or not _is_indeed_url(url):
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").removeprefix("www.")
        job_key = parse_qs(parsed.query).get("jk", [url])[0]
        key = (host, job_key)
        if key not in checked and (expired := _indeed_url_is_expired(client, url)) is not None:
            checked[key] = expired
        if checked.get(key):
            return True
    return False


def _is_indeed_url(url: str | None) -> bool:
    if not url:
        return False
    hostname = urlparse(url).hostname or ""
    return hostname.startswith("indeed.") or ".indeed." in hostname


def _indeed_url_is_expired(client: httpx.Client, url: str) -> bool | None:
    try:
        response = client.get(url)
    except httpx.HTTPError:
        return None
    if (
        response.status_code != 200
        or "text/html" not in response.headers.get("content-type", "")
        or not _is_indeed_url(str(response.url))
        or response.headers.get("cf-mitigated", "").strip().lower() == "challenge"
    ):
        return None
    return _indeed_html_is_expired(response.text)


def _indeed_html_is_expired(html: str) -> bool:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).casefold()
    return INDEED_EXPIRED_MESSAGE in text


@contextmanager
def _quiet_known_noisy_loggers(site: str) -> Iterator[None]:
    if site != "glassdoor":
        yield
        return
    logger = logging.getLogger("jobspy.glassdoor")
    original_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(original_level)


@contextmanager
def _quiet_known_noisy_loggers_for_sites(sites: tuple[str, ...]) -> Iterator[None]:
    if "glassdoor" not in sites:
        yield
        return
    with _quiet_known_noisy_loggers("glassdoor"):
        yield


def _site_location(site: str, location: str, config: SearchConfig) -> str:
    if site == "glassdoor":
        fallback_term = config.jobspy.glassdoor_regional_fallback_term
        override = config.jobspy.glassdoor_location_override
        if fallback_term and override and fallback_term.casefold() in location.casefold():
            return _glassdoor_location(override)
        return _glassdoor_location(location)
    return location


def _glassdoor_location(location: str) -> str:
    city, _, country = location.partition(",")
    if country.strip().casefold() == "france":
        return city.strip()
    return location.strip()


def _google_search_term(site: str, query: str, location: str, config: SearchConfig) -> str | None:
    if site != "google":
        return None
    excluded = " ".join(f"-{term}" for term in config.jobspy.google_query_exclude_terms)
    places = " OR ".join(config.jobspy.google_query_places) or f'"{location}"'
    return f"{query} emploi ({places}) {excluded}"


def _google_search_term_for_sites(
    sites: tuple[str, ...],
    query: str,
    location: str,
    config: SearchConfig,
) -> str | None:
    if "google" not in sites:
        return None
    return _google_search_term("google", query, location, config)
