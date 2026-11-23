from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TypeGuard
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.contracts import infer_offer_contract
from scrapper.dates import FRENCH_DATE_PHRASE_RE
from scrapper.models import JobOffer
from scrapper.scrapers import cadremploi, france_travail
from scrapper.scrapers.async_http import HostThrottle, get_checked_page
from scrapper.scrapers.common import (
    apply_url,
    first_text,
    job_location,
    json_ld_jobposting,
    meta_content,
    offer_matches_search,
    org_name,
    posted_date_is_before_cutoff,
)
from scrapper.source_rules import get_source_rules
from scrapper.source_rules.cadremploi import offer_id as cadremploi_offer_id
from scrapper.source_rules.meteojob import is_placeholder_offer
from scrapper.text import (
    clean_text,
    external_url_key,
    hostname_matches,
    is_application_destination,
)


async def _extract_detail_offer(
    client: httpx.AsyncClient,
    url: str,
    source: str,
    query: str,
    config: SearchConfig,
    throttle: HostThrottle | None = None,
    *,
    cutoff: datetime | None = None,
) -> JobOffer | None:
    if source == "france_travail":
        return await france_travail.extract_public_offer(client, url, query, config, throttle)
    detail_url = url
    if source == "isarta":
        job_id = parse_qs(urlparse(url).query).get("job", [""])[0]
        if job_id.isdigit():
            detail_url = f"https://isarta.fr/cgi-bin/emplois/pm.cgi?action=display&login={job_id}"
    if source == "cadremploi":
        response = await cadremploi.get_page(client, detail_url, throttle)
    else:
        response = await get_checked_page(client, detail_url, throttle)
    if response.status_code in {403, 404, 410}:
        return None
    response.raise_for_status()
    if "text/html" not in response.headers.get("content-type", ""):
        return None
    if _page_provider_is_blocked(source, response.text, config):
        return None

    soup = BeautifulSoup(response.text, "lxml")
    job = json_ld_jobposting(soup) or {}
    text = soup.get_text("\n", strip=True)
    source_rules = get_source_rules(source)
    title = source_rules.normalize_title(
        clean_text(job.get("title"))
        or source_rules.extract_title(soup, url)
        or _clean_title(first_text(soup, ("h1",)))
        or meta_content(soup, "og:title", "twitter:title")
    )
    company = (
        source_rules.extract_company(soup, url)
        or org_name(job.get("hiringOrganization"))
        or _company_from_page(source, soup, text, url)
    )
    location = (
        source_rules.extract_location(soup, url)
        or job_location(job.get("jobLocation"))
        or _infer_location(text, config)
    )
    source_description = source_rules.extract_description(soup, url)
    if source == "emploi_collectivites" and not source_description:
        return None
    structured_description = clean_text(job.get("description"))
    if source == "jobijoba":
        source_description = structured_description or source_description
    description = source_description or structured_description or _description_from_page(soup)
    employment_type = source_rules.extract_employment_type(soup, url) or _employment_type(job.get("employmentType"))
    if not employment_type and source != "jobijoba":
        employment_type = infer_offer_contract(title, description)
    offer = JobOffer(
        source=source,
        source_url=url,
        title=title,
        company=company,
        location=location,
        description=description,
        date_posted=source_rules.extract_date(soup, url) or clean_text(job.get("datePosted")) or _infer_date(text),
        employment_type=employment_type,
        salary=source_rules.extract_salary(soup, url) or _infer_salary(text) or clean_text(job.get("baseSalary")),
        is_remote=True if job.get("jobLocationType") == "TELECOMMUTE" else None,
        company_url=source_rules.extract_company_url(soup, url) or _site_root(url),
        search_query=query,
        search_location=config.location_label,
        scraped_at=datetime.now(UTC).isoformat(),
        raw={"json_ld": job, "url": url},
    )
    if source == "jobijoba" and cutoff is not None and not _offer_is_usable(offer, config, cutoff=cutoff):
        return None
    direct_url = await _direct_url_for_detail(client, soup, url, source, throttle)
    if _direct_url_is_blocked(source, direct_url, config):
        return None
    offer.source_id = _offer_source_id(source, url, direct_url)
    offer.direct_url = direct_url or url
    return offer


async def _direct_url_for_detail(
    client: httpx.AsyncClient,
    soup: BeautifulSoup,
    url: str,
    source: str,
    throttle: HostThrottle | None = None,
) -> str | None:
    direct_url = apply_url(soup, url)
    if source != "jobijoba" or not direct_url:
        return direct_url
    resolved = await _jobijoba_redirect_target(client, direct_url, throttle)
    return resolved or direct_url


async def _jobijoba_redirect_target(
    client: httpx.AsyncClient,
    redirect_url: str,
    throttle: HostThrottle | None = None,
) -> str | None:
    try:
        response = await get_checked_page(client, redirect_url, throttle)
    except httpx.HTTPError:
        return None
    if response.status_code >= 400:
        return None
    response_target = _jobijoba_external_target(str(response.url))
    if response_target:
        return response_target
    if "text/html" not in response.headers.get("content-type", ""):
        return None
    soup = BeautifulSoup(response.text, "lxml")
    explicit_application = _jobijoba_external_target(apply_url(soup, redirect_url))
    if explicit_application:
        return explicit_application
    for anchor in soup.select('a[href*="from="]'):
        href = anchor.get("href")
        target = _jobijoba_external_target(href) if isinstance(href, str) else None
        if target:
            return target
    return None


def _jobijoba_external_target(url: str | None) -> str | None:
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or hostname_matches(parsed.hostname, "jobijoba.com")
    ):
        return None
    from_url = parse_qs(parsed.query).get("from", [None])[0]
    target = unquote(from_url) if from_url else url
    return target if is_application_destination(target) else None


def _direct_url_is_blocked(source: str, direct_url: str | None, config: SearchConfig) -> bool:
    if not direct_url:
        return False
    spec = next((item for item in config.aggregators.direct_specs if item.source == source), None)
    if not spec or not spec.blocked_direct_hosts:
        return False
    host = urlparse(direct_url).hostname
    return any(hostname_matches(host, blocked) for blocked in spec.blocked_direct_hosts)


def _page_provider_is_blocked(source: str, html: str, config: SearchConfig) -> bool:
    spec = next((item for item in config.aggregators.direct_specs if item.source == source), None)
    if not spec or not spec.blocked_provider_terms:
        return False
    detail = re.search(
        r"['\"]detail['\"]\s*:\s*\{.*?['\"]customerName['\"]\s*:\s*['\"]([^'\"]+)",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    if not detail:
        return False
    provider = detail.group(1).casefold()
    return any(term.casefold() in provider for term in spec.blocked_provider_terms)


def _offer_is_usable(
    offer: JobOffer | None,
    config: SearchConfig,
    *,
    cutoff: datetime | None = None,
) -> TypeGuard[JobOffer]:
    if not offer or not offer.title:
        return False
    if cutoff is not None:
        public_offer = (offer.raw or {}).get("public_offer")
        modified = public_offer.get("dateDerniereModification") if isinstance(public_offer, dict) else None
        date = offer.date_posted or modified
        if posted_date_is_before_cutoff(date, cutoff):
            return False
    if _is_meteojob_placeholder_offer(offer):
        return False
    return offer_matches_search(offer, config, allow_description_location=True)


def _is_meteojob_placeholder_offer(offer: JobOffer) -> bool:
    if getattr(offer, "source", None) != "meteojob":
        return False
    raw_value = getattr(offer, "raw", None)
    raw = raw_value if isinstance(raw_value, dict) else {}
    return is_placeholder_offer(
        getattr(offer, "title", None),
        getattr(offer, "company", None),
        getattr(offer, "date_posted", None),
        has_jobposting=bool(raw.get("json_ld")),
    )


def _clean_title(title: str | None) -> str | None:
    if not title:
        return None
    parts = [part.strip() for part in title.split("\n") if part.strip()]
    return parts[-1] if parts else title


def _company_from_page(source: str, soup: BeautifulSoup, text: str, url: str) -> str | None:
    if source == "example_direct_board":
        node = soup.select_one("[data-test='company-name'], .media-heading, .t4")
        if node:
            return clean_text(node.get_text(" "))
    if source == "jobijoba":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            if line.upper() in {"CDI", "CDD"} and index + 1 < len(lines):
                return clean_text(lines[index + 1])
    return _company_from_domain(url)


def _description_from_page(soup: BeautifulSoup) -> str | None:
    return first_text(
        soup, ("[itemprop='description']", ".description", "#description", "main", "article")
    ) or meta_content(soup, "description", "og:description")


def _employment_type(value: object) -> str | None:
    if isinstance(value, list):
        return ", ".join(filter(None, [clean_text(item) for item in value])) or None
    return clean_text(value)


def _infer_location(text: str | None, config: SearchConfig | None = None) -> str | None:
    if config is None:
        config = SearchConfig()
    normalized = (clean_text(text) or "").lower()
    for term in config.aggregators.location_terms:
        if term in normalized:
            return term.title()
    return None


def _infer_date(text: str | None) -> str | None:
    cleaned = clean_text(text) or ""
    for marker in ("Actualisé le", "Publiée le", "Publié le"):
        if marker in cleaned:
            match = FRENCH_DATE_PHRASE_RE.search(cleaned.split(marker, 1)[1])
            return clean_text(match.group(0)) if match else None
    return None


def _infer_salary(text: str | None) -> str | None:
    cleaned = clean_text(text) or ""
    if "€" not in cleaned and "Euros" not in cleaned:
        return None
    for marker in ("Salaire :", "Salaire"):
        if marker in cleaned:
            return cleaned.split(marker, 1)[1][:80].strip()
    return None


def _company_from_domain(url: str) -> str | None:
    host = urlparse(url).netloc.lower().removeprefix("www.")
    parts = host.split(".")
    if len(parts) >= 2:
        return parts[-2].replace("-", " ").title()
    return host.title() if host else None


def _source_id(url: str) -> str:
    return urlparse(url).path.rstrip("/").split("/")[-1] or url


def _offer_source_id(source: str, url: str, direct_url: str | None) -> str | None:
    if source == "cadremploi":
        return cadremploi_offer_id(url)
    if source == "jobijoba":
        direct_key = external_url_key(direct_url, blocked_host="jobijoba.com")
        if direct_key:
            return direct_key
    return _source_id(url)


def _site_root(url: str) -> str | None:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}/" if parsed.scheme and parsed.netloc else None
