from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable
from typing import Mapping
from urllib.parse import parse_qs, unquote, urlparse

from bs4 import BeautifulSoup


SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
DESCRIPTION_DEDUPE_MIN_LENGTH = 80
CONTRACT_MARKERS_RE = re.compile(
    r"\b(h/?f|f/?h|m/?f|f/?m|cdi|cdd|stage|alternance|apprentissage)\b",
    re.IGNORECASE,
)
EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(offre|poste|mission|recrutement|candidat(?:e)?|profil)\s+(?:de|en|pour)\s+stage\b",
        r"\bstage\s+(?:de|d[’']|conventionne|a pourvoir|a partir|au sein)\b",
        r"\bstagiaire\b",
        r"\ben\s+alternance\b",
        r"\balternance\s+(?:de|niveau|bac|a pourvoir|au sein|rattache|rattachée)\b",
        r"\brecherch\w*\s+(?:un|une|notre)?\s*(?:\(?e\)?\s*)?alternant(?:e)?\b",
        r"\balternant(?:e)?\s+(?:example|sample|demo|placeholder|role|specialist|coordinator|manager)\b",
        r"\bcontrat\s+d[’']apprentissage\b",
        r"\bcontrat\s+de\s+professionnalisation\b",
        r"\bcontrat\s+professionnel\b",
        r"\bapprentissage\s+(?:de|niveau|bac|a pourvoir|au sein)\b",
        r"\bouvert\w*\s+(?:aux|a tous les)?\s*apprenti(?:s|es)?\b",
        r"\bmissions?\s+confiees?\s+au\s+stagiaire\b",
        r"\bmissions?\s+confiées?\s+au\s+stagiaire\b",
    )
)


def clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ")
    text = SPACE_RE.sub(" ", text).strip()
    return text or None


def normalize_text(value: object | None) -> str:
    text = clean_text(value) or ""
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = CONTRACT_MARKERS_RE.sub(" ", text)
    text = NON_WORD_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def stable_hash(*parts: object | None) -> str:
    payload = "|".join(normalize_text(part) for part in parts if part)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def dedupe_key(title: object | None, company: object | None, location: object | None = None) -> str:
    return dedupe_key_for_fields(
        {"title": title, "company": company, "location": location},
        fields=("title", "company"),
        fallback_fields=("location",),
    )


def dedupe_key_for_fields(
    record: Mapping[str, object | None],
    fields: Iterable[str],
    fallback_fields: Iterable[str] = (),
) -> str:
    field_names = tuple(fields)
    normalized_parts = [normalize_text(record.get(field_name)) for field_name in field_names]
    key_parts = list(normalized_parts)
    if any(not part for part in normalized_parts):
        key_parts.extend(normalize_text(record.get(field_name)) for field_name in fallback_fields)
    return stable_hash(*key_parts)


def description_dedupe_key(
    description: object | None,
    company: object | None = None,
    location: object | None = None,
) -> str | None:
    return description_dedupe_key_for_fields(
        {"description": description, "company": company, "location": location},
        fields=("description", "company", "location"),
    )


def description_dedupe_key_for_fields(
    record: Mapping[str, object | None],
    fields: Iterable[str],
) -> str | None:
    field_names = tuple(fields)
    if not field_names:
        return None

    description_norm = normalize_text(record.get(field_names[0]))
    if len(description_norm) < DESCRIPTION_DEDUPE_MIN_LENGTH:
        return None

    key_parts = [description_norm]
    for field_name in field_names[1:]:
        normalized = normalize_text(record.get(field_name))
        if normalized:
            key_parts.append(normalized)
    return stable_hash(*key_parts)


def url_key(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    query = parse_qs(parsed.query)
    relevant_params = []
    for name in ("jk", "jl", "currentJobId", "jobId", "gh_jid"):
        if query.get(name):
            relevant_params.append(f"{name}={query[name][0]}")
    compact_query = "&".join(relevant_params)
    return f"{parsed.netloc.lower()}{path}?{compact_query}".rstrip("?")


def unwrap_redirect(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    for key in ("url", "u", "target", "redirectUrl"):
        if query.get(key):
            return unquote(query[key][0])
    return url


def contains_excluded_terms(values: Iterable[object | None], exclude_terms: Iterable[str]) -> bool:
    haystack = " ".join(_normalize_for_match(value) for value in values if value)
    for term in exclude_terms:
        normalized = _normalize_for_match(term)
        if normalized and re.search(rf"(^|\s){re.escape(normalized)}($|\s)", haystack):
            return True
    return False


def has_excluded_contract_signal(
    title: object | None,
    employment_type: object | None,
    description: object | None,
    exclude_terms: Iterable[str],
) -> bool:
    if contains_excluded_terms((title, employment_type), exclude_terms):
        return True
    normalized_description = _normalize_for_match(description)
    if not normalized_description:
        return False
    return any(pattern.search(normalized_description) for pattern in EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS)


def contains_any_terms(values: Iterable[object | None], include_terms: Iterable[str]) -> bool:
    haystack = " ".join(_normalize_for_match(value) for value in values if value)
    for term in include_terms:
        normalized = _normalize_for_match(term)
        if normalized and re.search(rf"(^|\s){re.escape(normalized)}($|\s)", haystack):
            return True
    return False


def _normalize_for_match(value: object | None) -> str:
    text = clean_text(value) or ""
    text = text.replace("’", " ").replace("'", " ")
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = NON_WORD_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def completeness_score(record: dict[str, object | None]) -> int:
    score = 0
    weighted_fields = {
        "title": 8,
        "company": 8,
        "location": 5,
        "description": 1,
        "direct_url": 6,
        "date_posted": 4,
        "employment_type": 3,
        "salary": 4,
        "min_amount": 4,
        "company_url": 3,
        "company_description": 2,
    }
    for field, weight in weighted_fields.items():
        if record.get(field):
            score += weight
    description = clean_text(record.get("description"))
    if description:
        score += min(len(description), 4000) // 100
    return score
