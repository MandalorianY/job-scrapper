from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from bs4 import BeautifulSoup

from scrapper.source_rules import emploi_territorial, france_travail, jobijoba
from scrapper.source_rules.common import normalize_offer_title
from scrapper.text import clean_text

TrackerRepair = Callable[[object | None, object | None, object | None, object | None], tuple[str | None, str | None, str | None]]
SoupExtractor = Callable[[BeautifulSoup, str], str | None]


def _default_tracker_repair(
    title: object | None,
    company: object | None,
    location: object | None,
    _description: object | None,
) -> tuple[str | None, str | None, str | None]:
    return clean_text(title), clean_text(company), clean_text(location)


def _default_title_normalizer(value: object | None) -> str | None:
    return normalize_offer_title(value)


def _no_source_override(_soup: BeautifulSoup, _url: str) -> str | None:
    return None


@dataclass(frozen=True)
class SourceRuleSet:
    normalize_title: Callable[[object | None], str | None] = _default_title_normalizer
    repair_tracker_fields: TrackerRepair = _default_tracker_repair
    extract_title: SoupExtractor = _no_source_override
    extract_company: SoupExtractor = _no_source_override
    extract_company_url: SoupExtractor = _no_source_override
    extract_location: SoupExtractor = _no_source_override
    extract_description: SoupExtractor = _no_source_override
    extract_employment_type: SoupExtractor = _no_source_override
    extract_date: SoupExtractor = _no_source_override
    extract_salary: SoupExtractor = _no_source_override


SOURCE_RULES: dict[str, SourceRuleSet] = {
    "france_travail": SourceRuleSet(
        normalize_title=france_travail.normalize_title,
        repair_tracker_fields=france_travail.repair_tracker_fields,
        extract_company=france_travail.extract_company,
        extract_company_url=france_travail.extract_company_url,
        extract_location=france_travail.extract_location,
        extract_date=france_travail.extract_date,
    ),
    "jobijoba": SourceRuleSet(
        extract_company=jobijoba.extract_company,
        extract_location=jobijoba.extract_location,
        extract_description=jobijoba.extract_description,
        extract_employment_type=jobijoba.extract_employment_type,
        extract_date=jobijoba.extract_date,
        extract_salary=jobijoba.extract_salary,
    ),
    "emploi_territorial": SourceRuleSet(
        extract_title=emploi_territorial.extract_title,
        extract_company=emploi_territorial.extract_company,
        extract_company_url=emploi_territorial.extract_company_url,
        extract_location=emploi_territorial.extract_location,
        extract_description=emploi_territorial.extract_description,
        extract_employment_type=emploi_territorial.extract_employment_type,
        extract_date=emploi_territorial.extract_date,
    ),
}


def get_source_rules(source: str | None) -> SourceRuleSet:
    return SOURCE_RULES.get(str(source or "").strip(), SourceRuleSet())
