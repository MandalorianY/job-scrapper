from __future__ import annotations

from collections.abc import Mapping
from typing import SupportsFloat, SupportsIndex, cast

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers.common import offer_matches_search
from scrapper.text import has_excluded_contract_signal, is_application_destination


def row_to_offer(row: Mapping[str, object], site: str, query: str, search_location: str) -> JobOffer:
    direct_url = string_or_none(row.get("job_url_direct"))
    return JobOffer(
        source=site,
        source_id=string_or_none(row.get("id")),
        source_url=string_or_none(row.get("job_url")) or "",
        direct_url=direct_url if is_application_destination(direct_url) else None,
        title=string_or_none(row.get("title")),
        company=string_or_none(row.get("company")),
        location=string_or_none(row.get("location")),
        description=string_or_none(row.get("description")),
        date_posted=string_or_none(row.get("date_posted")),
        employment_type=string_or_none(row.get("job_type")),
        min_amount=_float_or_none(row.get("min_amount")),
        max_amount=_float_or_none(row.get("max_amount")),
        currency=string_or_none(row.get("currency")),
        interval=string_or_none(row.get("interval")),
        is_remote=_bool_or_none(row.get("is_remote")),
        company_url=string_or_none(row.get("company_url")),
        company_description=string_or_none(row.get("company_description")),
        emails=string_or_none(row.get("emails")),
        search_query=query,
        search_location=search_location,
        raw=dict(row),
    )


def offer_is_usable(offer: JobOffer, row: Mapping[str, object], config: SearchConfig) -> bool:
    extra_contract_labels = (row.get("listing_type"), row.get("job_level"))
    return offer_matches_search(offer, config) and not any(
        has_excluded_contract_signal(None, label, None, config.exclude_terms) for label in extra_contract_labels
    )


def string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        if str(value).lower() == "nan":
            return None
        return float(cast("str | bytes | bytearray | SupportsFloat | SupportsIndex", value))
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes"}:
        return True
    if str(value).lower() in {"false", "0", "no"}:
        return False
    return None
