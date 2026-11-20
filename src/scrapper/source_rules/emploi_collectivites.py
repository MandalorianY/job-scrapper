import re

from bs4 import BeautifulSoup

from scrapper.text import clean_text

LOCATION_RE = re.compile(r"Ce poste est à pourvoir à\s+(.+?)(?:\s*\.)?$", re.IGNORECASE)


def extract_location(soup: BeautifulSoup, _url: str) -> str | None:
    info = soup.select_one(".section_offre .row b")
    for line in info.stripped_strings if info else ():
        match = LOCATION_RE.fullmatch(line)
        if match:
            return clean_text(match[1])
    return None


def extract_description(soup: BeautifulSoup, _url: str) -> str | None:
    duties = soup.select_one("#texte_offre")
    description = clean_text(duties.get_text(" ")) if duties else None
    if description and "Contexte et environnement du poste" in description:
        # Some pages prepend a generic paragraph about a different vacancy.
        description = description[description.index("Contexte et environnement du poste") :]
    return description
