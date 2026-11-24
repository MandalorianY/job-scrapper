from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from jobsfr.offers.text import clean_text, normalize_text
from jobsfr.source_rules.common import normalize_offer_title

FRANCE_TRAVAIL_LOCATION_RE = re.compile(
    r"^\s*\d{2,3}\s*-\s*(?P<location>.+?)\s*-\s*Localiser avec Mappy\s*$",
    re.IGNORECASE,
)
DESCRIPTION_COMPANY_PATTERNS = (
    re.compile(
        r"^(?P<company>(?:L[ea]s?\s+)?[A-Z][A-Za-z0-9À-ÿ&'()./-]*(?:\s+[A-Za-z0-9À-ÿ&'()./-]+){0,7})\s+"
        r"(?:recherche|recrute|ouvre|est\s+une|est\s+un)\b"
    ),
)
PUBLISHED_PREFIX_RE = re.compile(r"^publi[ée]e?\s+le\s+", re.IGNORECASE)
EXPLICIT_WORKPLACE_RE = re.compile(
    r"\bLieu du poste\s*:\s*(?:Télétravail hybride\s*)?\(?([0-9]{5}[^\S\r\n]+[^()<>,.;\r\n]+)",
    re.IGNORECASE,
)


def normalize_offer_location(workplace: object | None) -> str | None:
    if isinstance(workplace, dict):
        workplace = (
            workplace.get("libelleCommuneLieuTravail")
            or workplace.get("libelleLieuTravail")
            or workplace.get("syntheseLieuTravail")
        )
    return extract_mappy_location(workplace) or clean_text(workplace)


def public_offer_location(workplace: object, description: object) -> str | None:
    explicit_location = EXPLICIT_WORKPLACE_RE.search(str(description or ""))
    return clean_text(explicit_location.group(1)) if explicit_location else normalize_offer_location(workplace)


def repair_tracker_fields(
    title: object | None,
    company: object | None,
    location: object | None,
    description: object | None,
) -> tuple[str | None, str | None, str | None]:
    clean_title = normalize_offer_title(title)
    clean_company = clean_text(company)
    clean_location = clean_text(location)

    mappy_location = extract_mappy_location(clean_company) or extract_mappy_location(clean_location)
    if mappy_location:
        clean_location = mappy_location
        if extract_mappy_location(clean_company):
            clean_company = None

    if clean_company and clean_title and normalize_text(clean_company) == normalize_text(clean_title):
        clean_company = None
    if clean_company and extract_mappy_location(clean_company):
        clean_company = None
    if clean_company and normalize_text(clean_company) in {"non renseigne", "non communique"}:
        clean_company = None
    if not clean_company:
        clean_company = infer_company_from_description(description)

    return clean_title, clean_company, clean_location


def extract_mappy_location(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    match = FRANCE_TRAVAIL_LOCATION_RE.match(text)
    if not match:
        return None
    return clean_text(match.group("location"))


def infer_company_from_description(value: object | None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    head = text.split(". ", 1)[0]
    for pattern in DESCRIPTION_COMPANY_PATTERNS:
        match = pattern.match(head)
        if match:
            company = clean_text(match.group("company"))
            if company and len(company) <= 80:
                return company
    return None


def extract_company(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_france_travail_url(url):
        return None
    heading = soup.find("h2", string=lambda value: clean_text(value) == "Employeur")
    if not heading or not heading.parent:
        return None
    media = heading.parent.find("div", class_="media", recursive=False)
    if not media:
        return None
    title = media.select_one(".media-body h3.title, h3.title")
    return clean_text(title.get_text(" ")) if title else None


def extract_company_url(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_france_travail_url(url):
        return None
    link = soup.select_one('div.media .media-body a[href*="/page-employeur/"]')
    href = link.get("href") if link else None
    return urljoin(url, href) if isinstance(href, str) and href else None


def extract_location(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_france_travail_url(url):
        return None
    title_complement = soup.select_one("p.title-complementary")
    if not title_complement:
        return None
    text = clean_text(title_complement.get_text(" "))
    return extract_mappy_location(text) or text


def extract_date(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_france_travail_url(url):
        return None
    node = soup.select_one("[itemprop='datePosted']")
    if not node:
        return None
    return clean_text(node.get("content")) or clean_text(PUBLISHED_PREFIX_RE.sub("", node.get_text(" ")))


def normalize_title(value: object | None) -> str | None:
    return normalize_offer_title(value)


def _is_france_travail_url(url: str) -> bool:
    return "francetravail.fr" in urlparse(url).netloc.lower()
