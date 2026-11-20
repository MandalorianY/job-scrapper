from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from scrapper.text import clean_text

ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+-]+Z?)?")
FRENCH_NUMERIC_DATE_RE = re.compile(r"\b(?P<day>\d{1,2})/(?P<month>\d{1,2})/(?P<year>\d{4})\b")
FRENCH_TEXT_DATE_RE = re.compile(
    r"\b(?P<day>\d{1,2})\s+"
    r"(?P<month>janvier|février|fevrier|mars|avril|mai|juin|juillet|août|aout|septembre|octobre|novembre|décembre|decembre)"
    r"(?:\s+(?P<year>\d{4}))?\b",
    re.IGNORECASE,
)
RELATIVE_DATE_RE = re.compile(
    r"(?:il\s+y\s+a|depuis|posted|published|publié(?:e)?\s+il\s+y\s+a)?\s*"
    r"(?P<count>\d+)\s*"
    r"(?P<unit>minute?s?|min|heure?s?|hour?s?|h|jour?s?|day?s?|j|d|semaine?s?|week?s?|mois|month?s?)\b",
    re.IGNORECASE,
)
TODAY_RE = re.compile(r"\b(aujourd'hui|today)\b", re.IGNORECASE)
YESTERDAY_RE = re.compile(r"\b(hier|yesterday)\b", re.IGNORECASE)
FRENCH_MONTHS = {
    "janvier": 1,
    "février": 2,
    "fevrier": 2,
    "mars": 3,
    "avril": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7,
    "août": 8,
    "aout": 8,
    "septembre": 9,
    "octobre": 10,
    "novembre": 11,
    "décembre": 12,
    "decembre": 12,
}
FRENCH_DATE_PHRASE_RE = re.compile(
    rf"""
    (?:
        il\s+y\s+a\s+\d+\s*(?:min|mn|minutes?|h|heures?|j|jours?|d|semaines?|mois)
        | aujourd'hui
        | hier
        | \d{{1,2}}/\d{{1,2}}/\d{{4}}
        | \d{{1,2}}\s+(?:{"|".join(FRENCH_MONTHS)})(?:\s+\d{{4}})?
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class ParsedPostedDate:
    day: date
    moment: datetime | None = None


def parse_posted_date(value: object, now: datetime | None = None) -> ParsedPostedDate | None:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if isinstance(value, datetime):
        moment = value if value.tzinfo else value.replace(tzinfo=UTC)
        return ParsedPostedDate(day=moment.date(), moment=moment)
    if isinstance(value, date):
        return ParsedPostedDate(day=value)
    text = clean_text(value)
    if not text:
        return None
    if TODAY_RE.search(text):
        return ParsedPostedDate(day=now.date(), moment=now)
    if YESTERDAY_RE.search(text):
        moment = now - timedelta(days=1)
        return ParsedPostedDate(day=moment.date(), moment=moment)
    relative = RELATIVE_DATE_RE.search(text)
    if relative:
        moment = now - _relative_delta(int(relative.group("count")), relative.group("unit"))
        return ParsedPostedDate(day=moment.date(), moment=moment)
    iso = ISO_DATE_RE.search(text)
    if iso:
        return _parse_iso_date(iso.group(0))
    numeric = FRENCH_NUMERIC_DATE_RE.search(text)
    if numeric:
        return ParsedPostedDate(
            day=date(int(numeric.group("year")), int(numeric.group("month")), int(numeric.group("day")))
        )
    french = FRENCH_TEXT_DATE_RE.search(text)
    if french:
        return _parse_french_text_date(french, now)
    return None


def absolute_posted_date(value: object, now: datetime | None = None) -> str | None:
    text = clean_text(value)
    if not text:
        return None
    parsed = parse_posted_date(text, now=now)
    if not parsed:
        return text
    if parsed.moment:
        return parsed.moment.isoformat()
    return parsed.day.isoformat()


def _relative_delta(count: int, unit: str) -> timedelta:
    normalized = unit.lower()
    if normalized.startswith(("min", "minute")):
        return timedelta(minutes=count)
    if normalized.startswith(("h", "heure", "hour")):
        return timedelta(hours=count)
    if normalized.startswith(("sem", "week")):
        return timedelta(weeks=count)
    if normalized.startswith(("mois", "month")):
        return timedelta(days=count * 30)
    return timedelta(days=count)


def _parse_iso_date(value: str) -> ParsedPostedDate | None:
    try:
        if "T" in value or " " in value.strip()[10:]:
            moment = datetime.fromisoformat(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=UTC)
            return ParsedPostedDate(day=moment.date(), moment=moment)
        return ParsedPostedDate(day=date.fromisoformat(value[:10]))
    except ValueError:
        return None


def _parse_french_text_date(match: re.Match[str], now: datetime) -> ParsedPostedDate | None:
    month = FRENCH_MONTHS.get(match.group("month").lower())
    if not month:
        return None
    year = int(match.group("year") or now.year)
    try:
        parsed = date(year, month, int(match.group("day")))
    except ValueError:
        return None
    if not match.group("year") and parsed > now.date():
        parsed = date(year - 1, month, int(match.group("day")))
    return ParsedPostedDate(day=parsed)
