from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from functools import lru_cache
from urllib.parse import parse_qs, urlencode, urlparse

from bs4 import BeautifulSoup

SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
MATCH_SEPARATORS = str.maketrans({character: " " for character in "'’‐‑‒–—―−"})
_COMPANY_LEGAL_SUFFIX_RE = re.compile(r" (?:s ?a ?s(?: ?u)?|s ?a ?r ?l|s ?a|s ?n ?c|e ?u ?r ?l)$")
DESCRIPTION_DEDUPE_MIN_LENGTH = 80
NON_APPLICATION_PATH_RE = re.compile(
    r"/[^/]*bookmark(?:/|$)|/editorial/conseils/|/media/guide-candidat(?:/|$)|"
    r"/candidats/placement\.php$|/[^/]*cvtheque[^/]*(?:/|$)",
    re.IGNORECASE,
)
GENERIC_LISTING_PATHS = frozenset(
    {
        "apply",
        "apply-now",
        "application",
        "article",
        "career",
        "careers",
        "detail",
        "details",
        "detail_offre",
        "emploi",
        "emplois",
        "front-jobs-detail",
        "home",
        "index",
        "job",
        "jobs",
        "offres",
        "postuler",
        "redirect",
        "view",
    }
)
JOB_ID_PARAMETERS = ("jk", "jl", "currentJobId", "jobId", "gh_jid", "job", "offreId", "annonceId", "id_job")
# These shared endpoints identify the job in a parameter whose name is otherwise ambiguous.
QUERY_LISTING_ROUTES = {
    ("inrecruitingfr.intervieweb.it", "/annunci.php"): "l",
    ("isarta.fr", "/cgi-bin/emplois/externe_candidature.cgi"): "login",
}
_NUMERIC_ANNOUNCEMENT_PATH = re.compile(r"(/fr/annonce/\d+)(?:-[^/]+)?/?$")
_ALMA_POSTING_PATH = re.compile(r"(/poste/[a-z0-9]{10})(?:-[^/]+)?/?$")
_TALENTSOFT_POSTING_PATH = re.compile(r"/offre-de-emploi/[^/]+_(\d+)\.aspx$", re.IGNORECASE)
CONTRACT_MARKERS_RE = re.compile(
    r"\b(h/?f|f/?h|m/?f|f/?m|cdi|cdd|stage|alternance|apprentissage)\b",
    re.IGNORECASE,
)


def clean_text(value: object | None) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    if "<" in text and ">" in text:
        text = BeautifulSoup(text, "lxml").get_text(" ")
    text = SPACE_RE.sub(" ", text).strip()
    return text or None


def town_name(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\s*\(\d{2,3}\s*-\s*[^()]+\)$", "", text)
    department_match = re.search(r"(?:^|\b)\d{2,3}\s*-\s*([^,]+)", text)
    if department_match:
        return _clean_town_name(department_match.group(1))
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) > 1 and re.match(r"^\d{1,4}(?:\s*(?:bis|ter))?\s+", parts[0], flags=re.IGNORECASE):
        return _clean_town_name(parts[1])
    return _clean_town_name(parts[0] if parts else text)


def _clean_town_name(value: str) -> str:
    town = clean_text(value) or ""
    town = re.sub(
        r"\s+\d+(?:er|e|eme|ème)?\s+arrondissement\b.*$|\s+\d{1,2}(?:er|e|eme|ème)$", "", town, flags=re.IGNORECASE
    )
    town = re.sub(r"\s+-\s+localiser\b.*$", "", town, flags=re.IGNORECASE)
    town = re.sub(r"^\d{5}\s+|\s*\([^()]*(?:\(\d{2,3}\))\)$|\s*\(\d{2,5}\)$", "", town)
    return town.strip()


def is_application_destination(url: object | None) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if not listing_url_key(url) or NON_APPLICATION_PATH_RE.search(parsed.path):
        return False
    host = (parsed.hostname or "").rstrip(".")
    if any(
        host == domain or host.endswith(f".{domain}")
        for domain in ("googletagmanager.com", "google-analytics.com", "itunes.apple.com", "apps.apple.com")
    ):
        return False
    return not (
        parsed.hostname in {"meteojob.com", "www.meteojob.com"}
        and parsed.path.startswith("/candidat/offres/offre-d-emploi-")
    )


def _normalize_words(text: str, *, strip_contract_markers: bool = False) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    if strip_contract_markers:
        text = CONTRACT_MARKERS_RE.sub(" ", text)
    text = NON_WORD_RE.sub(" ", text)
    return SPACE_RE.sub(" ", text).strip()


def normalize_text(value: object | None) -> str:
    return _normalize_words(clean_text(value) or "", strip_contract_markers=True)


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
    route_parameter = QUERY_LISTING_ROUTES.get(((parsed.hostname or "").removeprefix("www."), path))
    parameters = JOB_ID_PARAMETERS
    if route_parameter and route_parameter not in parameters:
        parameters = (*parameters, route_parameter)
    compact_query = urlencode([(name, query[name][0]) for name in parameters if name in query])
    return f"{parsed.netloc.lower()}{path}?{compact_query}".rstrip("?")


def listing_url_key(url: str | None) -> str | None:
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    key = url_key(url)
    leaf = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower().split(".", 1)[0]
    route = ((parsed.hostname or "").removeprefix("www."), parsed.path.rstrip("/"))
    if key and ("?" in key or (leaf and leaf not in GENERIC_LISTING_PATHS and route not in QUERY_LISTING_ROUTES)):
        return key
    return None


def posting_url_key(url: str | None) -> str | None:
    """Recognize stable requisition IDs without changing persisted listing keys."""
    key = listing_url_key(url)
    if not key or not url:
        return key
    parts = urlparse(url)
    host = (parts.hostname or "").removeprefix("www.")
    if host in {"jobs.implid.com", "careers.dcsit-group.com"} and (
        posting := _NUMERIC_ANNOUNCEMENT_PATH.fullmatch(parts.path)
    ):
        return f"{host}{posting[1]}"
    if host == "recrutement.alma.fr" and (posting := _ALMA_POSTING_PATH.fullmatch(parts.path)):
        return f"{host}{posting[1]}"
    if (host.endswith(".talent-soft.com") or host == "emploi.cea.fr") and (
        posting := _TALENTSOFT_POSTING_PATH.fullmatch(parts.path)
    ):
        return f"{host}/offre-de-emploi/{posting[1]}"
    return key


def offer_link_keys(source_url: object, direct_url: object) -> set[str]:
    links = [source_url]
    if is_application_destination(direct_url):
        links.append(direct_url)
    return {key for link in links if isinstance(link, str) and (key := listing_url_key(link))}


def hostname_matches(hostname: str | None, domain: str) -> bool:
    host = (hostname or "").lower().rstrip(".")
    domain = domain.lower().rstrip(".")
    return host == domain or host.endswith(f".{domain}")


def external_url_key(url: str | None, blocked_host: str) -> str | None:
    if not is_application_destination(url):
        return None
    parsed = urlparse(url or "")
    if hostname_matches(parsed.hostname, blocked_host):
        return None
    return url_key(url)


def contains_any_terms(values: Iterable[object | None], include_terms: Iterable[str]) -> bool:
    haystack = " ".join(normalize_match_text(value) for value in values if value is not None)
    return any(literal_term_pattern(term).search(haystack) is not None for term in include_terms)


@lru_cache(maxsize=512)
def literal_term_pattern(value: str) -> re.Pattern[str]:
    term = normalize_match_text(value)
    return re.compile(rf"(?<!\S){re.escape(term)}(?!\S)" if term else r"(?!)")


def normalize_match_text(value: object | None) -> str:
    text = clean_text(value) or ""
    return _normalize_words(text.translate(MATCH_SEPARATORS))


def company_name_key(name: str) -> str:
    return _COMPANY_LEGAL_SUFFIX_RE.sub("", normalize_match_text(name))


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
