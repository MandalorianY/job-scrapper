from __future__ import annotations

import re

from bs4 import BeautifulSoup

from scrapper.text import clean_text


CONTRACT_LABELS = {
    "alternance": "Alternance",
    "cdi": "CDI",
    "cdd": "CDD",
    "intérim": "Intérim",
    "interim": "Intérim",
}


def extract_company(soup: BeautifulSoup, _url: str) -> str | None:
    lines = _offer_lines(soup)
    if len(lines) < 3:
        return None
    after_location = lines[2]
    if _contract_label(after_location):
        if len(lines) > 3 and not _looks_like_metadata(lines[3]):
            return clean_text(lines[3])
        return None
    if not _looks_like_metadata(after_location):
        return clean_text(after_location)
    return None


def extract_location(soup: BeautifulSoup, _url: str) -> str | None:
    lines = _offer_lines(soup)
    return clean_text(lines[1]) if len(lines) > 1 else None


def extract_description(soup: BeautifulSoup, _url: str) -> str | None:
    node = soup.select_one(".offer.current .permalink-description, .permalink-description")
    return clean_text(node.get_text(" ")) if node else None


def extract_employment_type(soup: BeautifulSoup, _url: str) -> str | None:
    for line in _offer_lines(soup)[2:5]:
        contract = _contract_label(line)
        if contract:
            return contract
    return None


def extract_date(soup: BeautifulSoup, _url: str) -> str | None:
    for line in _offer_lines(soup):
        if line.lower().startswith("publiée le ") or line.lower().startswith("publié le "):
            return clean_text(re.sub(r"^publi[ée]e?\s+le\s+", "", line, flags=re.IGNORECASE))
    return None


def extract_salary(soup: BeautifulSoup, _url: str) -> str | None:
    for line in _offer_lines(soup)[:8]:
        if "€" in line:
            return clean_text(line)
    return None


def _offer_lines(soup: BeautifulSoup) -> list[str]:
    offer = soup.select_one(".offer.current") or soup.select_one(".permalink-open")
    if not offer:
        return []
    return [line.strip() for line in offer.get_text("\n", strip=True).splitlines() if line.strip()]


def _contract_label(value: str) -> str | None:
    return CONTRACT_LABELS.get((clean_text(value) or "").casefold())


def _looks_like_metadata(value: str) -> bool:
    text = clean_text(value) or ""
    lowered = text.casefold()
    return (
        _contract_label(text) is not None
        or "€" in text
        or lowered.startswith("publié")
        or lowered.startswith("description de l'offre")
        or lowered.startswith("postuler")
    )
