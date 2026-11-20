from __future__ import annotations

import logging
import re
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from urllib.parse import urlencode, urlparse

import httpx
import structlog
from bs4 import BeautifulSoup
from jobspy import scrape_jobs
from jobspy.exception import GlassdoorException
from jobspy.glassdoor import Glassdoor as JobSpyGlassdoor
from jobspy.glassdoor.constant import headers as glassdoor_headers
from jobspy.glassdoor.util import get_cursor_for_page, parse_compensation, parse_location
from jobspy.linkedin import LinkedIn as JobSpyLinkedIn
from jobspy.model import Country, JobPost, ScraperInput

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore
from scrapper.text import clean_text, has_excluded_contract_signal

LINKEDIN_RELATIVE_DATE_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute?s?|min|hour?s?|h|day?s?|d|week?s?|month?s?|"
    r"heure?s?|jour?s?|semaine?s?|mois)",
    re.IGNORECASE,
)
INDEED_EXPIRED_MESSAGE = "cette offre a expiré sur indeed"
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
    counts: dict[str, int] = {}
    enabled_sites = [site for site in config.jobspy_sites if config.is_source_enabled(site)]
    for site in enabled_sites:
        counts[site] = 0
    plans = _jobspy_search_plans(enabled_sites, config, store)
    total_searches = len(plans)
    log.info("jobspy_scrape_started", sites=enabled_sites, searches=total_searches)
    for search_index, plan in enumerate(plans, start=1):
        for site in plan.sites:
            store.start_search(site, plan.query, plan.location)
        try:
            plan_counts = _run_jobspy_plan(plan, config, store, progress, search_index, total_searches)
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
                if progress:
                    progress(
                        f"JobSpy batch failed [{search_index}/{total_searches}]: "
                        f"{plan.label} | {plan.query} | {plan.location} "
                        f"(error={_short_error(exc)}); retrying individually"
                    )
                fallback_plans = [plan_for_site for plan_for_site in _solo_plans(plan)]
                for fallback_index, fallback_plan in enumerate(fallback_plans, start=1):
                    try:
                        plan_counts = _run_jobspy_plan(
                            fallback_plan,
                            config,
                            store,
                            progress,
                            fallback_index,
                            len(fallback_plans),
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
                        _finish_failed_plan(fallback_plan, store, progress, fallback_index, len(fallback_plans), fallback_exc)
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
                if config.jobspy_batch_sites and site != "glassdoor":
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
) -> dict[str, int]:
    for site in plan.sites:
        _prepare_site_call(site, config)
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
    with _quiet_known_noisy_loggers_for_sites(plan.sites):
        jobs = scrape_jobs(
            site_name=plan.sites[0] if len(plan.sites) == 1 else list(plan.sites),
            search_term=plan.query,
            google_search_term=_google_search_term_for_sites(plan.sites, plan.query, plan.location, config),
            location=plan.location,
            distance=config.distance_km,
            results_wanted=plan.results_wanted,
            country_indeed=config.country,
            hours_old=plan.hours_old,
            linkedin_fetch_description=True,
            description_format="markdown",
            user_agent=config.user_agent,
            verbose=_jobspy_verbose_for_sites(plan.sites),
        )

    seen_by_site = dict.fromkeys(plan.sites, 0)
    stored_by_site = dict.fromkeys(plan.sites, 0)
    for row in jobs.to_dict(orient="records"):
        site = _row_site(row, plan)
        if site not in seen_by_site:
            continue
        seen_by_site[site] += 1
        offer = _row_to_offer(row, site, plan.query, plan.location, config)
        if not _offer_is_usable(offer, row, site, config):
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
            config,
            store,
            progress,
            search_index,
            total_searches,
        )
    return stored_by_site


def _finish_successful_site(
    site: str,
    query: str,
    location: str,
    seen: int,
    stored: int,
    config: SearchConfig,
    store: JobStore,
    progress: ProgressReporter | None,
    search_index: int,
    total_searches: int,
) -> None:
    error = _zero_rows_error(site, seen, config)
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
                f"(seen={seen}, stored={stored}, error=zero rows)"
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


def _zero_rows_error(site: str, seen: int, config: SearchConfig) -> str | None:
    if seen:
        return None
    if site == "google":
        return config.jobspy.google_zero_results_error
    if site == "glassdoor":
        return config.jobspy.glassdoor_zero_results_error
    return None


def _row_site(row: dict, plan: JobSpySearchPlan) -> str:
    if len(plan.sites) == 1:
        return plan.sites[0]
    value = _string_or_none(row.get("site"))
    return value.casefold() if value else ""


def _row_to_offer(row: dict, site: str, query: str, search_location: str, config: SearchConfig) -> JobOffer:
    return JobOffer(
        source=site,
        source_id=_string_or_none(row.get("id")),
        source_url=_string_or_none(row.get("job_url")) or "",
        direct_url=_string_or_none(row.get("job_url_direct")),
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
        raw=row,
    )


def _offer_is_usable(offer: JobOffer, row: dict, site: str, config: SearchConfig) -> bool:
    if has_excluded_contract_signal(
        row.get("title"),
        row.get("job_type") or row.get("listing_type"),
        row.get("description"),
        config.exclude_terms,
    ):
        return False
    if not config.title_is_allowed(row.get("title")):
        return False
    if config.is_company_excluded(site, row.get("company")):
        return False
    if not location_is_in_scope(offer.location, config.location_scope):
        return False
    if _is_expired_indeed_offer(offer, config):
        return False
    return bool(offer.source_url)


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
        return config.jobspy.glassdoor_results_wanted_per_query
    return config.results_wanted_per_query


def _is_expired_indeed_offer(offer: JobOffer, config: SearchConfig) -> bool:
    if offer.source != "indeed":
        return False
    for url in (offer.direct_url, offer.source_url):
        if _is_indeed_url(url) and _indeed_url_is_expired(url, config):
            return True
    return False


def _is_indeed_url(url: str | None) -> bool:
    if not url:
        return False
    return "indeed." in urlparse(url).netloc.lower()


def _indeed_url_is_expired(url: str, config: SearchConfig) -> bool:
    try:
        response = httpx.get(
            url,
            headers={"user-agent": config.user_agent, "accept-language": config.http.accept_language},
            follow_redirects=True,
            timeout=15,
        )
    except httpx.HTTPError:
        return False
    if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", ""):
        return False
    return _indeed_html_is_expired(response.text)


def _indeed_html_is_expired(html: str) -> bool:
    text = BeautifulSoup(html, "lxml").get_text(" ", strip=True).casefold()
    return INDEED_EXPIRED_MESSAGE in text


@contextmanager
def _quiet_known_noisy_loggers(site: str):
    if site != "glassdoor":
        yield
        return
    logger = logging.getLogger("JobSpy:Glassdoor")
    original_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        logger.setLevel(original_level)


@contextmanager
def _quiet_known_noisy_loggers_for_sites(sites: tuple[str, ...]):
    if "glassdoor" not in sites:
        yield
        return
    with _quiet_known_noisy_loggers("glassdoor"):
        yield


def _prepare_site_call(site: str, config: SearchConfig) -> None:
    if site == "linkedin":
        _patch_linkedin_scraper()
        return
    if site != "glassdoor":
        return
    _patch_glassdoor_scraper()
    glassdoor_url = Country.from_string(config.country).get_glassdoor_url().rstrip("/")
    glassdoor_domain = urlparse(glassdoor_url).netloc
    glassdoor_headers["authority"] = glassdoor_domain
    glassdoor_headers["origin"] = glassdoor_url
    glassdoor_headers["referer"] = f"{glassdoor_url}/"


def _patch_glassdoor_scraper() -> None:
    scrape_jobs.__globals__["Glassdoor"] = TolerantGlassdoor


def _patch_linkedin_scraper() -> None:
    scrape_jobs.__globals__["LinkedIn"] = TolerantLinkedIn


class TolerantLinkedIn(JobSpyLinkedIn):
    def _process_job(self, job_card, job_id: str, full_descr: bool) -> JobPost | None:
        job_post = super()._process_job(job_card, job_id, full_descr)
        if job_post and not job_post.date_posted:
            job_post.date_posted = _linkedin_date_posted(job_card)
        return job_post


def _linkedin_date_posted(job_card) -> date | None:
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
    if raw_datetime:
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


class TolerantGlassdoor(JobSpyGlassdoor):
    def _get_location(self, location: str, is_remote: bool) -> tuple[int, str]:
        if not location or is_remote:
            return super()._get_location(location, is_remote)
        params = urlencode({
            "locationTypeFilters": "CITY,STATE,COUNTRY",
            "caller": "jobs",
            "term": location,
        })
        response = self.session.get(f"{self.base_url}/autocomplete/location?{params}", timeout_seconds=15)
        if response.status_code != 200:
            raise GlassdoorException(f"Glassdoor location lookup returned HTTP {response.status_code}")
        locations = response.json()
        if not locations:
            raise GlassdoorException("Glassdoor location lookup returned no matches")
        match = locations[0]
        location_type = {"C": "CITY", "S": "STATE", "N": "COUNTRY"}.get(match.get("locationType"))
        if not location_type:
            raise GlassdoorException("Glassdoor location lookup returned an unsupported location type")
        return int(match["locationId"]), location_type

    def _get_csrf_token(self):
        # JobSpy 1.1.82 returns a trailing slash for country domains, while its
        # request methods add another slash. Glassdoor rejects those URLs.
        self.base_url = self.base_url.rstrip("/")
        if self.user_agent:
            self.session.headers["user-agent"] = self.user_agent
        response = self.session.get(f"{self.base_url}/", timeout_seconds=15, allow_redirects=True)
        if response.status_code != 200:
            raise GlassdoorException(f"Glassdoor session initialization returned HTTP {response.status_code}")
        match = re.search(r'"token"\s*:\s*"([^"]+)"', response.text.replace('\\"', '"'))
        if not match:
            raise GlassdoorException("Glassdoor session page did not contain a CSRF token")
        return match.group(1)

    def _fetch_jobs_page(
        self,
        scraper_input: ScraperInput,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None,
    ) -> tuple[list[JobPost], str | None]:
        jobs = []
        self.scraper_input = scraper_input
        try:
            payload = self._add_payload(location_id, location_type, page_num, cursor)
            response = self.session.post(
                f"{self.base_url}/graph",
                timeout_seconds=15,
                data=payload,
            )
            if response.status_code != 200:
                raise GlassdoorException(f"bad response status code: {response.status_code}")
            res_json = response.json()[0]
            jobs_data = res_json.get("data", {}).get("jobListings", {}).get("jobListings")
            if not jobs_data:
                raise ValueError("Error encountered in API response")
        except Exception as exc:  # noqa: BLE001 - tolerate malformed or failed Glassdoor pages.
            logging.getLogger("JobSpy:Glassdoor").error("Glassdoor: %s", str(exc))
            return jobs, None

        with ThreadPoolExecutor(max_workers=self.jobs_per_page) as executor:
            future_to_job_data = {
                executor.submit(self._process_job, job): job for job in jobs_data
            }
            for future in as_completed(future_to_job_data):
                try:
                    job_post = future.result()
                    if job_post:
                        jobs.append(job_post)
                except Exception as exc:
                    raise GlassdoorException(f"Glassdoor generated an exception: {exc}") from exc

        return jobs, get_cursor_for_page(
            res_json["data"]["jobListings"].get("paginationCursors", []),
            page_num + 1,
        )

    def _process_job(self, job_data):
        job_id = job_data["jobview"]["job"]["listingId"]
        job_url = f"{self.base_url.rstrip('/')}/job-listing/j?jl={job_id}"
        if job_url in self.seen_urls:
            return None
        self.seen_urls.add(job_url)
        job = job_data["jobview"]
        title = job["job"]["jobTitleText"]
        company_name = job["header"]["employerNameFromSearch"]
        company_id = job["header"]["employer"]["id"]
        location_name = job["header"].get("locationName", "")
        location_type = job["header"].get("locationType", "")
        age_in_days = job["header"].get("ageInDays")
        is_remote, location = False, None
        date_posted = (datetime.now(UTC) - timedelta(days=age_in_days)).date() if age_in_days is not None else None

        if location_type == "S":
            is_remote = True
        else:
            location = parse_location(location_name)

        company_logo = job_data["jobview"].get("overview", {}).get("squareLogoUrl", None)
        listing_type = (
            job_data["jobview"]
            .get("header", {})
            .get("adOrderSponsorshipLevel", "")
            .lower()
        )
        return JobPost(
            id=f"gd-{job_id}",
            title=title,
            company_url=f"{self.base_url.rstrip('/')}/Overview/W-EI_IE{company_id}.htm" if company_id else None,
            company_name=company_name,
            date_posted=date_posted,
            job_url=job_url,
            location=location,
            compensation=parse_compensation(job["header"]),
            is_remote=is_remote,
            description=None,
            emails=None,
            company_logo=company_logo,
            listing_type=listing_type,
        )


def _site_location(site: str, location: str, config: SearchConfig) -> str:
    if site == "glassdoor" and config.jobspy.glassdoor_regional_fallback_term in location.lower():
        return _glassdoor_location(config.jobspy.glassdoor_location_override)
    if site == "glassdoor":
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
    places = " OR ".join(config.jobspy.google_query_places)
    return f'{query} emploi ({places}) {excluded}'


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
        return float(value)
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
