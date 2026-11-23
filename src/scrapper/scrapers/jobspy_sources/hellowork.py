from __future__ import annotations

from typing import Unpack

from bs4 import BeautifulSoup, Tag
from curl_cffi.requests import Response
from curl_cffi.requests.session import RequestParams
from jobspy import SCRAPER_MAPPING
from jobspy.hellowork import HelloWork
from jobspy.model import JobPost, Site

from scrapper.scrapers.common import json_ld_jobposting
from scrapper.text import normalize_match_text

_CONTRACTS = {
    "cdi",
    "cdd",
    "interim",
    "independant",
    "freelance",
    "stage",
    "alternance",
    "apprentissage",
    "fonctionnaire",
    "franchise",
    "associe",
    "stage de lycee",
}
_REMOTE_LABELS = {"teletravail partiel", "teletravail occasionnel", "teletravail complet"}


def patch_hellowork_scraper() -> None:
    SCRAPER_MAPPING[Site.HELLOWORK] = DetailedHelloWork


class DetailedHelloWork(HelloWork):
    def _process_job(self, card: Tag) -> JobPost | None:
        offer = super()._process_job(card)
        if offer and (contract := card.select_one("[data-cy='contractCard']")):
            offer.listing_type = offer.listing_type or contract.get_text(" ", strip=True)
        return offer

    def _fetch_details(self, job_id: str, job_url: str) -> dict[str, object]:
        response: Response | None = None
        get = self.session.get

        def capture_response(url: str, **kwargs: Unpack[RequestParams]) -> Response:
            nonlocal response
            response = get(url, **kwargs)
            return response

        # JobSpy's HelloWork session has no response hooks. Reuse its detail request.
        self.session.get = capture_response
        try:
            details = super()._fetch_details(job_id, job_url)
        finally:
            self.session.get = get
        if response is not None and response.status_code == 200:
            details.update(page_metadata(response.text))
        return details


def page_metadata(html: str) -> dict[str, object]:
    soup = BeautifulSoup(html, "lxml")
    heading = soup.select_one("h1")
    if not heading or not heading.parent or not heading.parent.parent:
        return {}
    # These tags sit next to the vacancy title, outside the cards for suggested offers.
    summary = heading.parent.parent
    metadata: dict[str, object] = {}
    for node in summary.select("li, .tag-secondary-s"):
        label = node.get_text(" ", strip=True)
        normalized = normalize_match_text(label)
        if normalized in _CONTRACTS:
            metadata.setdefault("listing_type", label)
        if normalized in _REMOTE_LABELS:
            metadata["is_remote"] = True
            metadata["work_from_home_type"] = label
    salary = summary.select_one("[data-cy='salary-tag-button']")
    if salary and '"Salaire réel"' in str(salary.get("data-analytics-values-param")):
        advertised = HelloWork._parse_salary(salary.get_text(" ", strip=True))
        if advertised:
            metadata.update(advertised.model_dump())
    posting = json_ld_jobposting(soup) or {}
    if posting.get("jobLocationType") == "TELECOMMUTE":
        metadata["is_remote"] = True
    return metadata
