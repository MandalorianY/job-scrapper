from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import urlsplit

type OfferRejection = Literal["incomplete", "contract", "filters", "location"]


@dataclass(slots=True)
class JobOffer:
    source: str
    source_url: str
    title: str | None = None
    company: str | None = None
    location: str | None = None
    description: str | None = None
    direct_url: str | None = None
    source_id: str | None = None
    date_posted: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    min_amount: float | None = None
    max_amount: float | None = None
    currency: str | None = None
    interval: str | None = None
    is_remote: bool | None = None
    company_url: str | None = None
    company_description: str | None = None
    emails: str | None = None
    search_query: str | None = None
    search_location: str | None = None
    scraped_at: str | None = None
    raw: dict[str, object] | None = None

    def has_source(self) -> bool:
        try:
            page = urlsplit(self.source_url)
        except ValueError:
            return False
        return bool(self.source.strip() and page.scheme in {"http", "https"} and page.hostname)

    def to_record(self) -> dict[str, object]:
        record = asdict(self)
        record["scraped_at"] = record["scraped_at"] or datetime.now(UTC).isoformat()
        return record
