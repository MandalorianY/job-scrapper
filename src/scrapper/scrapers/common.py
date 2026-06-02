from __future__ import annotations

import json
from collections.abc import Iterable
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from scrapper.text import clean_text

APPLY_SIGNAL_RE = re.compile(
    r"\b(?:apply|application|postul\w*|candid\w*|rejoindre|join)\b",
    re.IGNORECASE,
)
NON_APPLY_SIGNAL_RE = re.compile(
    r"\b(?:share|partage|facebook|twitter|linkedin|whatsapp|mailto|alerte|alert|saved?|favori|login|connexion|onetap|bookmark)\b",
    re.IGNORECASE,
)


def absolutize(base_url: str, href: str | None) -> str | None:
    if not href:
        return None
    if href.startswith("mailto:") or href.startswith("tel:"):
        return None
    return urljoin(base_url, href)


def apply_url(soup: BeautifulSoup | Tag, page_url: str) -> str | None:
    candidates: list[tuple[int, str]] = []
    for node in soup.select("a[href], form[action]"):
        href = node.get("href") if node.name == "a" else node.get("action")
        url = absolutize(page_url, str(href) if href else None)
        if not url or not _is_candidate_apply_url(url):
            continue
        signal = _apply_signal_text(node, url)
        if NON_APPLY_SIGNAL_RE.search(signal):
            continue
        score = _apply_score(signal, url, page_url)
        if node.name == "a":
            score += 10
        if score > 0:
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


def _is_candidate_apply_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _apply_signal_text(node: Tag, url: str) -> str:
    values = [
        node.get_text(" "),
        url,
        str(node.get("aria-label") or ""),
        str(node.get("title") or ""),
        str(node.get("id") or ""),
        " ".join(str(value) for value in node.get("class", [])),
        str(node.get("data-testid") or ""),
        str(node.get("data-cy") or ""),
    ]
    return " ".join(values)


def _apply_score(signal: str, url: str, page_url: str) -> int:
    score = 0
    if APPLY_SIGNAL_RE.search(signal):
        score += 100
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


def json_ld_jobposting(soup: BeautifulSoup) -> dict | None:
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


def _find_jobposting(payload) -> dict | None:
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


def org_name(value) -> str | None:
    if isinstance(value, dict):
        return clean_text(value.get("name"))
    return clean_text(value)


def job_location(value) -> str | None:
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


def next_data(soup: BeautifulSoup) -> dict | None:
    node = soup.select_one("script#__NEXT_DATA__")
    if not node:
        return None
    try:
        return json.loads(node.string or node.get_text())
    except json.JSONDecodeError:
        return None
