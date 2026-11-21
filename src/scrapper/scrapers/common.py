from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urldefrag, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from scrapper.config import SearchConfig
from scrapper.dates import parse_posted_date
from scrapper.location_scope import location_is_in_scope, mentions_location_in_scope
from scrapper.models import JobOffer
from scrapper.text import clean_text, has_excluded_contract_signal, is_application_destination

APPLY_SIGNAL_RE = re.compile(
    r"\b(?:apply|application|postul\w*|candidatur\w*|candidater|rejoindre|join)\b",
    re.IGNORECASE,
)
NON_APPLY_SIGNAL_RE = re.compile(
    r"\b(?:share|partage|facebook|twitter|linkedin|whatsapp|mailto|alerte|alert|saved?|"
    r"favori|login|connexion|onetap\w*|\w*bookmark|guide-candidat|conseils-candidature|placement|cvtheque\w*)\b",
    re.IGNORECASE,
)


def offer_matches_search(
    offer: JobOffer,
    config: SearchConfig,
    *,
    require_details: bool = True,
    allow_description_location: bool = False,
) -> bool:
    if not offer.source_url or (require_details and not (offer.description or offer.employment_type)):
        return False
    if not config.title_is_allowed(offer.title) or config.is_company_excluded(offer.source, offer.company):
        return False
    if has_excluded_contract_signal(offer.title, offer.employment_type, offer.description, config.exclude_terms):
        return False
    if not offer.location and allow_description_location:
        return mentions_location_in_scope(offer.description, config.location_scope)
    return location_is_in_scope(offer.location, config.location_scope)


def absolutize(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith(("mailto:", "tel:")):
        return None
    return urljoin(base_url, href)


def apply_url(soup: BeautifulSoup | Tag, page_url: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    listing_url = urldefrag(page_url).url
    for node in soup.select("a[href], form[action], [data-redirect]"):
        if node.has_attr("data-redirect"):
            href = node.get("data-redirect")
        elif node.name == "a":
            href = node.get("href")
        else:
            href = node.get("action")
        url = absolutize(page_url, str(href) if href else None)
        if not url or urldefrag(url).url == listing_url or not is_application_destination(url):
            continue
        signal = _apply_signal_text(node)
        path = urlparse(url).path
        if NON_APPLY_SIGNAL_RE.search(f"{signal} {path}"):
            continue
        if not APPLY_SIGNAL_RE.search(signal) and not (node.name == "form" and APPLY_SIGNAL_RE.search(path)):
            continue
        score = _apply_score(url, page_url)
        if node.name == "a":
            score += 10
        candidates.append((score, url))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def first_text(soup: BeautifulSoup | Tag, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        node = soup.select_one(selector)
        if node:
            text = clean_text(node.get_text(" "))
            if text:
                return text
    return None


def _apply_signal_text(node: Tag) -> str:
    values = [
        node.get_text(" "),
        str(node.get("aria-label") or ""),
        str(node.get("title") or ""),
    ]
    return " ".join(values)


def _apply_score(url: str, page_url: str) -> int:
    score = 0
    if APPLY_SIGNAL_RE.search(url):
        score += 40
    if urlparse(url).netloc.lower() != urlparse(page_url).netloc.lower():
        score += 15
    return score


def meta_content(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        node = soup.select_one(f'meta[name="{name}"], meta[property="{name}"]')
        if node and node.get("content"):
            return clean_text(node.get("content"))
    return None


def json_ld_jobposting(soup: BeautifulSoup) -> dict[str, object] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_jobposting(payload)
        if found:
            return found
    return None


def _find_jobposting(payload: object) -> dict[str, object] | None:
    if isinstance(payload, dict):
        kind = payload.get("@type")
        if kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind):
            return payload
        for value in payload.values():
            found = _find_jobposting(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _find_jobposting(item)
            if found:
                return found
    return None


def org_name(value: object) -> str | None:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def job_location(value: object) -> str | None:
    if isinstance(value, list):
        return " / ".join(filter(None, [job_location(item) for item in value])) or None
    if isinstance(value, dict):
        address = value.get("address")
        if isinstance(address, dict):
            parts = [
                address.get("addressLocality"),
                address.get("addressRegion"),
                address.get("addressCountry"),
            ]
            return clean_text(", ".join(str(part) for part in parts if part))
        return clean_text(value.get("name"))
    return clean_text(value)


def next_data(soup: BeautifulSoup) -> dict[str, object] | None:
    node = soup.select_one("script#__NEXT_DATA__")
    if not node:
        return None
    try:
        payload: object = json.loads(node.string or node.get_text())
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


class SearchHistory(Protocol):
    def get_last_success(self, source: str, query: str, location: str) -> datetime | None: ...


def custom_search_cutoff(
    store: SearchHistory,
    source: str,
    query: str,
    location: str,
    config: SearchConfig,
    now: datetime | None = None,
) -> datetime:
    now = now or datetime.now(UTC)
    last_success = store.get_last_success(source, query, location)
    if not last_success:
        return now - timedelta(hours=config.default_hours_old)
    if last_success.tzinfo is None:
        last_success = last_success.replace(tzinfo=UTC)
    return last_success - timedelta(hours=config.overlap_hours)


def posted_date_is_before_cutoff(value: object, cutoff: datetime, now: datetime | None = None) -> bool:
    parsed = parse_posted_date(value, now=now)
    if not parsed:
        return False
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=UTC)
    if parsed.moment:
        return parsed.moment < cutoff
    return parsed.day < cutoff.date()
