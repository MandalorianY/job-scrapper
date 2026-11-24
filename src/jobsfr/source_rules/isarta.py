import re

from bs4 import BeautifulSoup

from jobsfr.offers.text import clean_text


def extract_location(soup: BeautifulSoup, _url: str) -> str | None:
    location = soup.select_one("h3.Lieu")
    return clean_text(location.get_text(" ")) if location else None


def extract_date(soup: BeautifulSoup, _url: str) -> str | None:
    match = re.search(r"Publiée\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", soup.get_text(" "))
    return match[1] if match else None


def extract_employment_type(soup: BeautifulSoup, _url: str) -> str | None:
    match = re.search(r"Type d'emploi\s*:\s*(CDI|CDD|Intérim)\b", soup.get_text(" "), re.IGNORECASE)
    return match[1] if match else None
