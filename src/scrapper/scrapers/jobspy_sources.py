from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import partial
from typing import SupportsFloat, SupportsIndex, cast
from urllib.parse import parse_qs, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup, Tag
from jobspy import SCRAPER_MAPPING, scrape_jobs
from jobspy.linkedin import LinkedIn as JobSpyLinkedIn
from jobspy.linkedin.util import criteria
from jobspy.model import JobPost, JobType, ScraperInput, Site
from jobspy.util import description_fields, get_enum_from_job_type
from requests import Response
from requests.exceptions import RequestException

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers.common import json_ld_jobposting, offer_matches_search
from scrapper.storage import JobStore
from scrapper.text import clean_text, has_excluded_contract_signal, is_application_destination

JOBSPY_SITES = ("indeed", "linkedin", "glassdoor", "google", "hellowork")
LINKEDIN_RELATIVE_DATE_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute?s?|min|hour?s?|h|day?s?|d|week?s?|month?s?|"
    r"heure?s?|jour?s?|semaine?s?|mois)",
    re.IGNORECASE,
)
INDEED_EXPIRED_MESSAGE = "cette offre a expiré sur indeed"
DETAIL_FETCH_SITES = {"glassdoor", "hellowork"}
KM_PER_MILE = 1.609344
ProgressReporter = Callable[[str], None]
log = structlog.get_logger(__name__)


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
            plan_counts = _run_jobspy_plan(plan, config, store, progress, search_index, total_searches, is_expired_indeed)
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
    return [
        JobSpySearchPlan(
            sites=(site,),
            query=plan.query,
            location=plan.location,
            hours_old=plan.hours_old,
            results_wanted=plan.results_wanted,
        )
        for site in plan.sites
    ]


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
        _patch_linkedin_scraper()
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

    seen_by_site = dict.fromkeys(plan.sites, 0)
    stored_by_site = dict.fromkeys(plan.sites, 0)
    for row in jobs.to_dict(orient="records"):
        site = _row_site(row, plan)
        if site not in seen_by_site:
            continue
        seen_by_site[site] += 1
        offer = _row_to_offer(row, site, plan.query, plan.location, config)
        if not _offer_is_usable(offer, row, config) or is_expired_indeed(offer):
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
    value = _string_or_none(row.get("site"))
    return value.casefold() if value else ""


def _row_to_offer(
    row: Mapping[str, object], site: str, query: str, search_location: str, config: SearchConfig
) -> JobOffer:
    direct_url = _string_or_none(row.get("job_url_direct"))
    return JobOffer(
        source=site,
        source_id=_string_or_none(row.get("id")),
        source_url=_string_or_none(row.get("job_url")) or "",
        direct_url=direct_url if is_application_destination(direct_url) else None,
        title=_string_or_none(row.get("title")),
        company=_string_or_none(row.get("company")),
        location=_string_or_none(row.get("location")),
        description=_string_or_none(row.get("description")),
        date_posted=_string_or_none(row.get("date_posted")),
        employment_type=_string_or_none(row.get("job_type")),
        min_amount=_float_or_none(row.get("min_amount")),
        max_amount=_float_or_none(row.get("max_amount")),
        currency=_string_or_none(row.get("currency")),
        interval=_string_or_none(row.get("interval")),
        is_remote=_bool_or_none(row.get("is_remote")),
        company_url=_string_or_none(row.get("company_url")),
        company_description=_string_or_none(row.get("company_description")),
        emails=_string_or_none(row.get("emails")),
        search_query=query,
        search_location=search_location,
        raw=dict(row),
    )


def _offer_is_usable(offer: JobOffer, row: Mapping[str, object], config: SearchConfig) -> bool:
    extra_contract_labels = (row.get("listing_type"), row.get("job_level"))
    return (
        offer_matches_search(offer, config)
        and not any(has_excluded_contract_signal(None, label, None, config.exclude_terms) for label in extra_contract_labels)
    )


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


def _patch_linkedin_scraper() -> None:
    SCRAPER_MAPPING[Site.LINKEDIN] = TolerantLinkedIn


class TolerantLinkedIn(JobSpyLinkedIn):
    def scrape(self, scraper_input: ScraperInput) -> list[JobPost]:
        # The batch shares its input across boards, so enable detail requests on LinkedIn only.
        return super().scrape(scraper_input.model_copy(update={"fetch_description": True}))

    def _process_job(self, job_card: Tag, job_id: str) -> JobPost:
        job_post = super()._process_job(job_card, job_id)
        if job_post and not job_post.date_posted:
            job_post.date_posted = _linkedin_date_posted(job_card)
        return job_post

    def _fetch_details(self, job_id: str) -> dict[str, object]:
        primary_response: Response | None = None

        def capture_response(response: Response, **_kwargs: object) -> None:
            nonlocal primary_response
            primary_response = response

        # Reuse JobSpy's fields and inspect the same response for localized criteria.
        response_hooks = self.session.hooks["response"]
        response_hooks.append(capture_response)
        try:
            details = super()._fetch_details(job_id)
        finally:
            response_hooks.remove(capture_response)
        if details and primary_response is not None and not (details.get("description") and details.get("job_type")):
            self._fill_missing_details(details, primary_response.text)
        if details.get("description") and details.get("job_type"):
            return details
        try:
            response = self.session.get(f"{self.base_url}/jobs-guest/jobs/api/jobPosting/{job_id}")
        except RequestException:
            return details
        if response.status_code == 200:
            self._fill_missing_details(details, response.text)
        return details

    def _fill_missing_details(self, details: dict[str, object], html: str) -> None:
        soup = BeautifulSoup(html, "lxml")
        job = json_ld_jobposting(soup) or {}
        if not details.get("description"):
            description_node = soup.select_one(".show-more-less-html__markup")
            description = str(description_node) if description_node else _string_or_none(job.get("description"))
            if description:
                details.update(description_fields(description, self.scraper_input.description_format))
        if not details.get("job_type"):
            value = criteria(soup, "Employment type") or criteria(soup, "Type d") or job.get("employmentType")
            localized = {
                "temps plein": JobType.FULL_TIME,
                "temps partiel": JobType.PART_TIME,
                "intérim": JobType.TEMPORARY,
                "contrat": JobType.CONTRACT,
                "stage": JobType.INTERNSHIP,
            }
            label = (_string_or_none(value) or "").casefold()
            if has_excluded_contract_signal(
                None, label, None, ("stage", "alternance", "apprentissage", "intern", "internship", "apprenticeship")
            ):
                job_type = JobType.INTERNSHIP
            else:
                job_type = localized.get(label) or get_enum_from_job_type(label.replace("-", "").replace(" ", ""))
            if job_type:
                details["job_type"] = [job_type]


def _linkedin_date_posted(job_card: Tag) -> date | None:
    metadata_card = job_card.find("div", class_="base-search-card__metadata")
    if not metadata_card:
        return None
    datetime_tag = metadata_card.find(
        "time",
        class_=lambda value: bool(value and "job-search-card__listdate" in value),
    )
    if not datetime_tag:
        return None
    raw_datetime = datetime_tag.get("datetime")
    if isinstance(raw_datetime, str) and raw_datetime:
        try:
            return date.fromisoformat(raw_datetime[:10])
        except ValueError:
            pass
    return _linkedin_relative_date(clean_text(datetime_tag.get_text(" ")))


def _linkedin_relative_date(value: str | None, now: datetime | None = None) -> date | None:
    text = (value or "").strip().casefold()
    if not text:
        return None
    now = now or datetime.now(UTC)
    if "yesterday" in text or "hier" in text:
        return (now - timedelta(days=1)).date()
    if "today" in text or "aujourd'hui" in text or "aujourdhui" in text:
        return now.date()
    match = LINKEDIN_RELATIVE_DATE_RE.search(text)
    if not match:
        return None
    count = int(match.group("count"))
    unit = match.group("unit")
    if unit.startswith("min"):
        posted = now - timedelta(minutes=count)
    elif unit.startswith(("h", "hour", "heure")):
        posted = now - timedelta(hours=count)
    elif unit.startswith(("d", "day", "jour")):
        posted = now - timedelta(days=count)
    elif unit.startswith(("w", "week", "semaine")):
        posted = now - timedelta(weeks=count)
    elif unit.startswith(("month", "mois")):
        posted = now - timedelta(days=30 * count)
    else:
        return None
    return posted.date()


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


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        if str(value).lower() == "nan":
            return None
        return float(cast("str | bytes | bytearray | SupportsFloat | SupportsIndex", value))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes"}:
        return True
    if str(value).lower() in {"false", "0", "no"}:
        return False
    return None
