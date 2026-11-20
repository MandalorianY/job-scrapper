from __future__ import annotations

import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from scrapper.text import clean_text

DESCRIPTION_SECTIONS = (
    "Descriptif de l'emploi",
    "Missions / conditions d'exercice",
    "Profils recherchés",
)
PUBLISHED_DATE_RE = re.compile(r"\bPubliée?\s+le\s+(?P<date>\d{2}/\d{2}/\d{4})\b", re.IGNORECASE)


def extract_title(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    heading = soup.select_one("main h2.set-line-emploi, main h2")
    title = clean_text(heading.get_text(" ")) if heading else None
    if title:
        return title
    if soup.title:
        page_title = clean_text(soup.title.get_text(" "))
        if page_title:
            return page_title.rsplit(" | ", 1)[0].rsplit(" - ", 1)[0]
    return None


def extract_company(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    return _label_value(soup, "Employeur")


def extract_company_url(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    node = _label_value_node(soup, "Site web de l'employeur")
    link = node.select_one("a[href]") if node else None
    href = link.get("href") if link else None
    return urljoin(url, href) if href else None


def extract_location(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    return _label_value(soup, "Lieu de travail")


def extract_description(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    sections = []
    for label in DESCRIPTION_SECTIONS:
        value = _section_text_after_label(soup, label)
        if value:
            sections.append(f"{label}: {value}")
    return clean_text("\n\n".join(sections))


def extract_employment_type(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    return _label_value(soup, "Type d'emploi")


def extract_date(soup: BeautifulSoup, url: str) -> str | None:
    if not _is_emploi_territorial_url(url):
        return None
    label_value = _label_value(soup, "Publiée le")
    if label_value:
        return label_value
    for node in soup.select(".row"):
        match = PUBLISHED_DATE_RE.search(clean_text(node.get_text(" ")) or "")
        if match:
            return match.group("date")
    return None


def _label_value(soup: BeautifulSoup, label: str) -> str | None:
    node = _label_value_node(soup, label)
    return clean_text(node.get_text(" ")) if node else None


def _label_value_node(soup: BeautifulSoup, label: str) -> Tag | None:
    label_node = soup.find(
        class_="offre-item-label",
        string=lambda value: (clean_text(value) or "").casefold() == label.casefold(),
    )
    if not label_node:
        return None
    container = label_node.parent
    if not isinstance(container, Tag):
        return None
    return container.select_one(".offre-item-value, .offre-item-text")


def _section_text_after_label(soup: BeautifulSoup, label: str) -> str | None:
    label_node = soup.find(
        class_="offre-item-label",
        string=lambda value: (clean_text(value) or "").casefold() == label.casefold(),
    )
    if not label_node:
        return None
    container = label_node.parent
    if not isinstance(container, Tag):
        return None
    sibling = container.find_next_sibling()
    if not isinstance(sibling, Tag):
        return None
    if "offre-item-text" not in sibling.get("class", []):
        return None
    return clean_text(sibling.get_text(" "))


def _is_emploi_territorial_url(url: str) -> bool:
    return "emploi-territorial.fr" in urlparse(url).netloc.lower()
