from __future__ import annotations

from collections.abc import Mapping

from jobsfr.config import SearchConfig
from jobsfr.offers.filters import has_excluded_contract_signal
from jobsfr.offers.models import JobOffer, OfferRejection
from jobsfr.offers.text import is_application_destination
from jobsfr.scrapers.common import float_or_none, offer_rejection


def row_to_offer(row: Mapping[str, object], site: str, query: str, search_location: str) -> JobOffer:
    direct_url = string_or_none(row.get("job_url_direct"))
    employment_type = string_or_none(row.get("listing_type")) if site == "hellowork" else None
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
        employment_type=employment_type or string_or_none(row.get("job_type")),
        min_amount=float_or_none(row.get("min_amount"), nan_is_missing=True),
        max_amount=float_or_none(row.get("max_amount"), nan_is_missing=True),
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


def jobspy_offer_rejection(offer: JobOffer, row: Mapping[str, object], config: SearchConfig) -> OfferRejection | None:
    rejection = offer_rejection(offer, config)
    extra_contract_labels = (row.get("listing_type"), row.get("job_level"))
    if any(has_excluded_contract_signal(None, label, None) for label in extra_contract_labels) and offer.has_source():
        return "contract"
    return rejection


def string_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    return text


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
