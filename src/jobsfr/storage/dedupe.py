from __future__ import annotations

import json
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from functools import cached_property
from typing import Protocol, cast
from urllib.parse import urlparse

import pandas as pd

from jobsfr.geography.scope import normalize_location_text
from jobsfr.offers.contracts import contract_evidence
from jobsfr.offers.salary import salary_bounds
from jobsfr.offers.text import (
    company_name_key,
    description_dedupe_key_for_fields,
    is_application_destination,
    normalize_text,
    posting_url_key,
    provider_listing_key,
    town_name,
)

_SYNDICATION_DOMAINS = frozenset(
    {
        "adzuna.fr",
        "appcast.io",
        "emploi-collectivites.fr",
        "francetravail.fr",
        "glassdoor.fr",
        "glassdoor.com",
        "hellowork.com",
        "hrm.tc",
        "indeed.com",
        "jobijoba.com",
        "linkedin.com",
        "meteojob.com",
        "talent.com",
    }
)
_EMPLOYER_PREFIX = re.compile(r"^(?:la|le|les|l|groupe) ")
_ROMAN_NUMERALS = {
    roman: str(number)
    for number, roman in enumerate(
        (
            "i",
            "ii",
            "iii",
            "iv",
            "v",
            "vi",
            "vii",
            "viii",
            "ix",
            "x",
            "xi",
            "xii",
            "xiii",
            "xiv",
            "xv",
            "xvi",
            "xvii",
            "xviii",
            "xix",
            "xx",
        ),
        1,
    )
}
_GENDER_MARKER = re.compile(r"\b[fhmdx](?:[/.-][fhmndbx]+){1,3}\b", re.IGNORECASE)
_UNKNOWN_COMPANIES = {"enseigne non disponible", "confidentiel", "anonyme"}
_POSTED_DAY = re.compile(r"\d{4}-\d{2}-\d{2}(?=$|[T ])")
_CONTRACT_TYPE = re.compile(r"\b(?:cdi|cdd|freelance|interim|stage|alternance|apprentissage)\b")
_PUBLIC_VACANCY_SUMMARY = re.compile(r"^(?:detail du poste )?employeur .+date de publication\b")


class _OfferRow(Protocol):
    source: str
    source_url: str
    source_id: str | None
    direct_url: str | None
    location: str | None
    description_key: str | None
    dedupe_key: str | None

    def _asdict(self) -> dict[str, object]: ...


def _location_key(value: object | None) -> str:
    return normalize_location_text(town_name(value)) if isinstance(value, str) else ""


def _apply_destination(url: str | None) -> tuple[str, str] | None:
    if not url or not (key := posting_url_key(url)):
        return None
    host = (urlparse(url).hostname or "").removeprefix("www.")
    if any(host == domain or host.endswith(f".{domain}") for domain in _SYNDICATION_DOMAINS):
        return None
    return host, key


def _numbered_employer_matches(first: str, second: str) -> bool:
    first_name, _, first_suffix = first.rpartition(" ")
    second_name, _, second_suffix = second.rpartition(" ")
    return bool(
        first_name
        and first_name == second_name
        and (_ROMAN_NUMERALS.get(first_suffix) == second_suffix or _ROMAN_NUMERALS.get(second_suffix) == first_suffix)
    )


@dataclass(frozen=True)
class _PostingEvidence:
    title: str
    company: str
    city: str
    body: str
    day: str
    contracts: frozenset[str]

    @classmethod
    def from_row(cls, fields: dict[str, object], city: str) -> _PostingEvidence:
        raw_title, raw_company = fields.get("title"), fields.get("company")
        title = raw_title if isinstance(raw_title, str) else ""
        company = company_name_key(raw_company) if isinstance(raw_company, str) else ""
        if company in _UNKNOWN_COMPANIES or (
            fields.get("source") == "jobijoba" and company in {"web", "france travail"}
        ):
            company = ""
        day = _POSTED_DAY.match(str(fields.get("date_posted") or ""))
        contracts = _CONTRACT_TYPE.findall(
            f"{normalize_location_text(title)} {normalize_location_text(fields.get('employment_type'))}"
        )
        description = fields.get("description")
        return cls(
            normalize_text(_GENDER_MARKER.sub(" ", title.replace("&", " et "))),
            company,
            city,
            normalize_text(description) if isinstance(description, str) else "",
            day[0] if day else "",
            frozenset(contracts),
        )

    @cached_property
    def employer_name(self) -> str:
        return _EMPLOYER_PREFIX.sub("", self.company)

    @cached_property
    def shingles(self) -> set[tuple[str, ...]]:
        words = self.body.split()
        return {tuple(words[index : index + 5]) for index in range(len(words) - 4)}

    def bucket_keys(self) -> list[tuple[str, str, str]]:
        if not self.city or not self.title:
            return []
        keys = [("title", self.city, self.title)]
        if self.employer_name:
            keys.append(("company", self.city, self.employer_name))
        return keys

    def matches(self, other: _PostingEvidence, *, same_source: bool) -> bool:
        if self.contracts and other.contracts and self.contracts.isdisjoint(other.contracts):
            return False
        comparable_company = bool(self.company and other.company)
        same_company = comparable_company and self.employer_name == other.employer_name
        if not same_company:
            smaller, larger = sorted((self.employer_name, other.employer_name), key=len)
            related_company = len(smaller) >= 4 and larger.startswith(smaller + " ")
            related_company = related_company or _numbered_employer_matches(smaller, larger)
            if self.title != other.title or (comparable_company and not related_company):
                return False
            shorter = min(len(self.shingles), len(other.shingles))
            shared = len(self.shingles & other.shingles)
            return shared >= 60 and shared >= 0.92 * shorter

        if self.title != other.title:
            if min(len(self.title.split()), len(other.title.split())) < 4:
                return False
            if re.findall(r"\b\d+\b", self.title) != re.findall(r"\b\d+\b", other.title):
                return False
            if SequenceMatcher(None, self.title, other.title, autojunk=False).ratio() < 0.93:
                return False

        if self.body == other.body and len(self.body) >= 80:
            return True
        if 160 <= min(len(self.body), len(other.body)) and max(len(self.body), len(other.body)) <= 850:
            compact_ratio = SequenceMatcher(
                None, self.body.replace(" ", ""), other.body.replace(" ", ""), autojunk=False
            )
            if compact_ratio.ratio() >= 0.97:
                return True

        shorter = min(len(self.shingles), len(other.shingles))
        shared = len(self.shingles & other.shingles)
        if shared >= 30 and shared >= 0.88 * shorter:
            return True

        # A board can replace the description with a summary and omit the employer's article.
        return (
            self.company != other.company
            and not same_source
            and self.title == other.title
            and len(self.title.split()) >= 6
            and bool(self.day and self.day == other.day)
            and bool(_PUBLIC_VACANCY_SUMMARY.match(self.body) or _PUBLIC_VACANCY_SUMMARY.match(other.body))
        )


def build_group_keys(
    frame: pd.DataFrame,
    dedupe_primary_fields: tuple[str, ...],
    dedupe_fallback_fields: tuple[str, ...],
    dedupe_description_fields: tuple[str, ...],
) -> pd.Series:
    parents = list(range(len(frame)))
    rows = cast("list[_OfferRow]", list(frame.itertuples(index=False)))
    sources = [{row.source} for row in rows]
    cities = [_location_key(row.location) for row in rows]
    locations = [{city} if city else set() for city in cities]
    apply_urls = [row.direct_url if is_application_destination(row.direct_url) else None for row in rows]
    destinations = [{destination} if (destination := _apply_destination(url)) else set() for url in apply_urls]
    use_location = any(
        "location" in fields for fields in (dedupe_primary_fields, dedupe_fallback_fields, dedupe_description_fields)
    )
    use_posting_evidence = (dedupe_primary_fields, dedupe_fallback_fields, dedupe_description_fields) == (
        ("title", "company"),
        ("location",),
        ("description", "company", "location"),
    )
    evidence = (
        [_PostingEvidence.from_row(row._asdict(), city) for row, city in zip(rows, cities, strict=True)]
        if use_posting_evidence
        else []
    )

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        parents[right] = left
        sources[left].update(sources[right])
        locations[left].update(locations[right])
        destinations[left].update(destinations[right])

    def compatible(left: int, right: int) -> bool:
        return not (
            (use_location and locations[left] and locations[right] and locations[left].isdisjoint(locations[right]))
            or any(
                left_host == right_host and left_key != right_key
                for left_host, left_key in destinations[left]
                for right_host, right_key in destinations[right]
            )
        )

    seen_identity: dict[tuple[str, ...], int] = {}
    for index, row in enumerate(rows):
        keys: list[tuple[str, ...]] = [
            ("url", key) for link in (row.source_url, apply_urls[index]) if (key := posting_url_key(link))
        ]
        if provider_key := provider_listing_key(row.source_url, row.source_id):
            keys.append(("id", row.source, *provider_key))
        for key in keys:
            left, right = find(index), find(seen_identity.setdefault(key, index))
            if left != right:
                union(left, right)

    candidates: dict[tuple[str, str], set[int]] = {}
    for index, row in enumerate(rows):
        fields = row._asdict()
        description_key = row.description_key
        if not isinstance(description_key, str) or not description_key:
            description_key = description_dedupe_key_for_fields(fields, fields=dedupe_description_fields)
        repost_key = None
        if dedupe_description_fields and all(
            isinstance(value, str) and value for value in (fields.get(key) for key in ("title", "company", "location"))
        ):
            repost_key = description_dedupe_key_for_fields(
                fields, fields=("description", "title", "company", "location")
            )
        for kind, value in (("repost", repost_key), ("title", row.dedupe_key), ("description", description_key)):
            if (
                not isinstance(value, str)
                or not value
                or (kind == "title" and evidence and not evidence[index].company)
            ):
                continue
            bucket = candidates.setdefault((kind, value), set())
            for previous in sorted(bucket):
                left, right = find(index), find(previous)
                if left == right or (kind != "repost" and sources[left] & sources[right]):
                    continue
                if compatible(left, right):
                    union(left, right)
            bucket.add(find(index))

    if evidence:
        fuzzy_candidates: dict[tuple[str, str, str], list[int]] = {}
        for index, posting in enumerate(evidence):
            keys = posting.bucket_keys()
            for previous in sorted({match for key in keys for match in fuzzy_candidates.get(key, ())}):
                left, right = find(index), find(previous)
                if (
                    left != right
                    and compatible(left, right)
                    and posting.matches(evidence[previous], same_source=rows[index].source == rows[previous].source)
                ):
                    union(left, right)
            for key in keys:
                fuzzy_candidates.setdefault(key, []).append(index)

    return pd.Series((str(find(index)) for index in range(len(frame))), index=frame.index, dtype="string")


def aggregate_group_links(group: pd.DataFrame) -> pd.Series:
    ranked = group.sort_values(
        by=["source_priority", "completeness_score", "scraped_at"],
        ascending=[True, False, False],
        kind="stable",
    )
    copies = ranked.to_dict("records")
    contract = max((contract_evidence(copy.get) for copy in copies), key=lambda evidence: evidence[0])[1]
    bounds: tuple[int | None, int | None] = (None, None)
    if contract != "Service civique":
        for copy in copies:
            bounds = salary_bounds(copy.get)
            if bounds != (None, None):
                break
    return pd.Series(
        {
            "sources": ", ".join(sorted({value for value in group["source"].dropna() if value})),
            "all_source_links": "\n".join(sorted({value for value in group["source_url"].dropna() if value})),
            "all_direct_links": _join_optional_links(group.loc[:, "direct_url"]),
            "group_description": max(group["description"].dropna(), key=len, default=None),
            "group_date_posted": _best_group_value(ranked, "date_posted"),
            "group_is_remote": _group_is_remote(group),
            "contract_type": contract,
            "annual_salary_bounds": bounds,
            "duplicate_count": len(group),
            "first_seen_at": group["first_seen_at"].min(),
            "last_seen_at": group["last_seen_at"].max(),
        }
    )


def _group_is_remote(group: pd.DataFrame) -> bool | None:
    values = group["is_remote"].dropna().to_list()
    if True in values:
        return True
    for raw in group["raw_json"].dropna():
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        posting = payload.get("json_ld") if isinstance(payload, dict) else None
        if isinstance(posting, dict) and posting.get("jobLocationType") == "TELECOMMUTE":
            return True
    return False if len(values) else None


def _best_group_value(group: pd.DataFrame, column: str) -> str | None:
    if column not in group.columns:
        return None
    for value in group[column]:
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "nat"}:
            return text
    return None


def _join_optional_links(values: pd.Series) -> str | None:
    deduped = sorted({value for value in values.dropna() if value})
    if not deduped:
        return None
    return "\n".join(deduped)
