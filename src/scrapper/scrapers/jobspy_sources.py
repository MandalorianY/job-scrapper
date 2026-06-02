from __future__ import annotations

import logging
import requests
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
import re
from typing import Tuple
from urllib.parse import urlparse

from jobspy import scrape_jobs
from jobspy.exception import GlassdoorException
from jobspy.glassdoor import Glassdoor as JobSpyGlassdoor
from jobspy.glassdoor.constant import headers as glassdoor_headers
from jobspy.glassdoor.util import get_cursor_for_page, parse_compensation, parse_location
from jobspy.linkedin import LinkedIn as JobSpyLinkedIn
from jobspy.model import Country
from jobspy.model import JobPost, ScraperInput

from scrapper.config import SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore
from scrapper.text import clean_text, contains_any_terms, has_excluded_contract_signal

LINKEDIN_RELATIVE_DATE_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute?s?|min|hour?s?|h|day?s?|d|week?s?|month?s?|"
    r"heure?s?|jour?s?|semaine?s?|mois)",
    re.IGNORECASE,
)


def _hours_old(store: JobStore, source: str, query: str, location: str, config: SearchConfig) -> int:
    if config.jobspy_hours_old_override:
        return config.jobspy_hours_old_override
    last_success = store.get_last_success(source, query, location)
    if not last_success:
        return config.default_hours_old
    now = datetime.now(timezone.utc)
    elapsed_hours = max(1, int((now - last_success).total_seconds() // 3600))
    return elapsed_hours + config.overlap_hours


def scrape_jobspy_sources(config: SearchConfig, store: JobStore) -> dict[str, int]:
    counts: dict[str, int] = {}
    for site in config.jobspy_sites:
        if not config.is_source_enabled(site):
            continue
        counts[site] = 0
        for query in config.queries:
            for configured_location in config.search_locations:
                search_location = _site_location(site, configured_location, config)
                store.start_search(site, query, search_location)
                seen = 0
                try:
                    _prepare_site_call(site, config)
                    with _quiet_known_noisy_loggers(site):
                        jobs = scrape_jobs(
                            site_name=site,
                            search_term=query,
                            google_search_term=_google_search_term(site, query, search_location, config),
                            location=search_location,
                            distance=config.distance_km,
                            results_wanted=_results_wanted(site, config),
                            country_indeed=config.country,
                            hours_old=_hours_old(store, site, query, search_location, config),
                            linkedin_fetch_description=True,
                            description_format="markdown",
                            user_agent=config.user_agent,
                            verbose=_jobspy_verbose(site),
                        )
                    for row in jobs.to_dict(orient="records"):
                        seen += 1
                        if has_excluded_contract_signal(
                            row.get("title"),
                            row.get("job_type") or row.get("listing_type"),
                            row.get("description"),
                            config.exclude_terms,
                        ):
                            continue
                        if not contains_any_terms((row.get("title"),), config.include_terms):
                            continue
                        offer = JobOffer(
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
                        if not location_is_in_scope(offer.location, config.location_scope):
                            continue
                        if offer.source_url:
                            store.upsert_offer(offer)
                            counts[site] += 1
                    if site == "google" and seen == 0:
                        store.finish_search(
                            site,
                            query,
                            search_location,
                            seen,
                            error=config.jobspy.google_zero_results_error,
                        )
                    elif site == "glassdoor" and seen == 0:
                        store.finish_search(
                            site,
                            query,
                            search_location,
                            seen,
                            error=config.jobspy.glassdoor_zero_results_error,
                        )
                    else:
                        store.finish_search(site, query, search_location, seen)
                except Exception as exc:  # noqa: BLE001 - continue with the other boards.
                    store.finish_search(site, query, search_location, seen, error=str(exc))
    return counts


def _jobspy_verbose(site: str) -> int | None:
    if site == "glassdoor":
        return None
    return 0


def _results_wanted(site: str, config: SearchConfig) -> int:
    if site == "glassdoor":
        return config.jobspy.glassdoor_results_wanted_per_query
    return config.results_wanted_per_query


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
            return datetime.strptime(raw_datetime, "%Y-%m-%d").date()
        except ValueError:
            pass
    return _linkedin_relative_date(clean_text(datetime_tag.get_text(" ")))


def _linkedin_relative_date(value: str | None, now: datetime | None = None) -> date | None:
    text = (value or "").strip().casefold()
    if not text:
        return None
    now = now or datetime.now()
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
    def _fetch_jobs_page(
        self,
        scraper_input: ScraperInput,
        location_id: int,
        location_type: str,
        page_num: int,
        cursor: str | None,
    ) -> Tuple[list[JobPost], str | None]:
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
        except (
            requests.exceptions.ReadTimeout,
            GlassdoorException,
            ValueError,
            Exception,
        ) as exc:
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
        job_url = f"{self.base_url}job-listing/j?jl={job_id}"
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
        date_posted = (datetime.now() - timedelta(days=age_in_days)).date() if age_in_days is not None else None

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
            company_url=f"{self.base_url}Overview/W-EI_IE{company_id}.htm" if company_id else None,
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
