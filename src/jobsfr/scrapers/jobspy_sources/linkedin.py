from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

from bs4 import BeautifulSoup, Tag
from jobspy import SCRAPER_MAPPING
from jobspy.linkedin import LinkedIn as JobSpyLinkedIn
from jobspy.linkedin.util import criteria
from jobspy.model import JobPost, JobType, ScraperInput, Site
from jobspy.util import description_fields, get_enum_from_job_type
from requests import Response
from requests.exceptions import RequestException

from jobsfr.offers.filters import has_excluded_contract_signal
from jobsfr.offers.text import clean_text
from jobsfr.scrapers.common import json_ld_jobposting
from jobsfr.scrapers.jobspy_sources.offers import string_or_none

LINKEDIN_RELATIVE_DATE_RE = re.compile(
    r"(?P<count>\d+)\s*(?P<unit>minute?s?|min|hour?s?|h|day?s?|d|week?s?|month?s?|"
    r"heure?s?|jour?s?|semaine?s?|mois)",
    re.IGNORECASE,
)


def patch_linkedin_scraper() -> None:
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
            description = str(description_node) if description_node else string_or_none(job.get("description"))
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
            label = (string_or_none(value) or "").casefold()
            if has_excluded_contract_signal(None, label, None):
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
