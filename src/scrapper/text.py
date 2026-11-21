from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Iterable, Mapping
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlencode, urlparse

from bs4 import BeautifulSoup

SPACE_RE = re.compile(r"\s+")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
DESCRIPTION_DEDUPE_MIN_LENGTH = 80
NON_APPLICATION_PATH_RE = re.compile(
    r"/[^/]*bookmark(?:/|$)|/editorial/conseils/|/media/guide-candidat(?:/|$)|"
    r"/candidats/placement\.php$|/[^/]*cvtheque[^/]*(?:/|$)",
    re.IGNORECASE,
)
GENERIC_LISTING_PATHS = frozenset({
    "apply", "apply-now", "application", "article", "career", "careers", "detail", "details",
    "emploi", "emplois", "home", "index", "job", "jobs", "offres", "postuler", "redirect", "view",
})
JOB_ID_PARAMETERS = ("jk", "jl", "currentJobId", "jobId", "gh_jid", "job", "offreId", "annonceId", "id_job")
# These shared endpoints identify the job in a parameter whose name is otherwise ambiguous.
QUERY_LISTING_ROUTES = {
    ("inrecruitingfr.intervieweb.it", "/annunci.php"): "l",
    ("isarta.fr", "/cgi-bin/emplois/externe_candidature.cgi"): "login",
    ("recrutement.lyon.fr", "/front-jobs-detail.html"): "id_job",
}
CONTRACT_MARKERS_RE = re.compile(
    r"\b(h/?f|f/?h|m/?f|f/?m|cdi|cdd|stage|alternance|apprentissage)\b",
    re.IGNORECASE,
)
EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(offre|poste|mission|recrutement|candidat(?:e)?|profil)\s+(?:de|en|pour)\s+stage\b",
        r"\bstage\s+(?:de|d |conventionne|a pourvoir|a partir|au sein)\b",
        r"\b(?:recherch\w*|recrut\w*)\s+(?:un|une|notre)\s+(?:e\s+)?stagiaire\b",
        r"\b(?:poste|offre|contrat|mission)\s+(?:est\s+|a pourvoir\s+)?en\s+alternance\b",
        r"\balternance\s+(?:de|niveau|bac|a pourvoir|au sein|rattache|rattachée)\b",
        r"\b(?:recherch\w*|cherch\w*|recrut\w*)\s+(?:un|une|notre)\s+(?:e\s+|futur\s+)?alternant(?:e)?\b",
        r"\balternant(?:e)?\s+(?:example|sample|demo|placeholder|role|specialist|coordinator|manager)\b",
        r"\bcontrat\s+d\s+apprentissage\b",
        r"\bcontrat\s+de\s+professionnalisation\b",
        r"\bcontrat\s+professionnel\b",
        r"\bapprentissage\s+(?:de|niveau|bac|a pourvoir|au sein)\b",
        r"\bouvert\w*\s+(?:aux|a tous les)?\s*apprenti(?:s|es)?\b",
        r"\bmissions?\s+confiees?\s+au\s+stagiaire\b",
        r"\bmissions?\s+confiées?\s+au\s+stagiaire\b",
        r"\b(?:type d alternance|type de contrat|contrat|emploi)\s+(?:en\s+)?(?:apprentissage|alternance|stage|internship|apprenticeship)\b",
        r"\b(?:ce|le|un)\s+poste\s+(?:est\s+)?benevole\b",
        r"\b(?:mission|poste|offre)\s+(?:de\s+|en\s+)?benevolat\b",
        r"\b(?:this|the)\s+(?:role|position)\s+is\s+(?:an?\s+)?(?:internship|apprenticeship)\b",
        r"\b(?:seeking|hiring|looking for)\s+(?:an?\s+)?(?:intern|apprentice)\b",
        r"\b(?:ce stage est|lieu du stage|duree du stage|gratification de stage)\b",
        r"(?<!y compris )\bdans le cadre d une alternance\b",
        r"\balternance\s+(?:a compter|sur\s+\d+\s+ans?)\b",
        r"^alternance\b",
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
    if any(host == domain or host.endswith(f".{domain}") for domain in ("googletagmanager.com", "google-analytics.com", "itunes.apple.com", "apps.apple.com")):
        return False
    return not (
        parsed.hostname in {"meteojob.com", "www.meteojob.com"}
        and parsed.path.startswith("/candidat/offres/offre-d-emploi-")
    )


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


def offer_link_keys(source_url: object, direct_url: object) -> set[str]:
    links = [source_url]
    if is_application_destination(direct_url):
        links.append(direct_url)
    return {key for link in links if isinstance(link, str) and (key := listing_url_key(link))}


def external_url_key(url: str | None, blocked_host: str) -> str | None:
    if not is_application_destination(url):
        return None
    parsed = urlparse(url or "")
    if blocked_host in parsed.netloc.lower():
        return None
    return url_key(url)


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
    terms = tuple(exclude_terms)
    # Match inflections explicitly; substring matching would also reject "internal".
    aliases = {
        "benevole": ("benevolat", "benevoles"),
        "volunteer": ("volunteering",),
        "stage": ("stages", "stagiaires"),
        "alternance": ("alternants", "alternantes"),
        "apprenti": ("apprentis", "apprenties"),
    }
    expanded = terms + tuple(alias for term in terms for alias in aliases.get(_normalize_for_match(term), ()))
    if contains_excluded_terms((title, employment_type), expanded):
        return True
    if _normalize_for_match(employment_type) in {"franchise", "franchise emploi non salarie"}:
        return True
    normalized_title = _normalize_for_match(title)
    if "alternance" in {_normalize_for_match(term) for term in terms} and re.search(
        r"(?:^alt\b|\ben alt\b|\b(?:bts|bachelor|master|mba)\b.*\balt\b)", normalized_title
    ):
        return True
    if re.search(r"\b(?:job|emploi|contrat)\s+etudiant\b", normalized_title):
        return True
    return _description_has_excluded_contract(None if description is None else str(description))


@lru_cache(maxsize=4096)
def _description_has_excluded_contract(description: str | None) -> bool:
    normalized_description = _normalize_for_match(description)
    if not normalized_description:
        return False
    if re.search(r"\bcreer votre entreprise\b", normalized_description) and re.search(
        r"\b(?:exercer votre activite de maniere independante|notre reseau de franchise)\b", normalized_description
    ):
        return True
    return any(pattern.search(normalized_description) for pattern in EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS)


def contains_any_terms(values: Iterable[object | None], include_terms: Iterable[str]) -> bool:
    haystack = " ".join(_normalize_for_match(value) for value in values if value)
    for term in include_terms:
        normalized = _normalize_for_match(term)
        if normalized and re.search(rf"(^|\s){re.escape(normalized)}($|\s)", haystack):
            return True
    return False


def matches_job_title(
    title: object | None, include_terms: Iterable[str], excluded_patterns: Iterable[str] = ()
) -> bool:
    """Require an included title term and reject configured, normalized title patterns."""
    return contains_any_terms((title,), include_terms) and not any(
        re.search(pattern, _normalize_for_match(title)) for pattern in excluded_patterns
    )


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
