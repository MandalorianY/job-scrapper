"""Evaluate configured filters against normalized listing fields."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from functools import cache, lru_cache
from pathlib import Path

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveInt,
    PrivateAttr,
    StrictBool,
    field_validator,
    model_validator,
)

from jobsfr.catalogs.company_types import CompanyCatalog, has_company_type
from jobsfr.offers.dates import parse_posted_date
from jobsfr.offers.salary import salary_bounds
from jobsfr.offers.text import literal_term_pattern, normalize_match_text

_EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS = tuple(
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


class RegexRule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    regex: str = Field(min_length=1)

    @field_validator("regex")
    @classmethod
    def _valid_regex(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}") from exc
        return value


type TextRule = str | RegexRule


def _rule_matches(rule: TextRule, text: str) -> bool:
    if isinstance(rule, str):
        return literal_term_pattern(rule).search(text) is not None
    return re.search(rule.regex, text) is not None


class TextFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    include: tuple[TextRule, ...] = ()
    exclude: tuple[TextRule, ...] = ()

    def allows(self, value: object) -> bool:
        return _text_allows(value, self.include, self.exclude)


def _text_allows(value: object, include: tuple[TextRule, ...], exclude: tuple[TextRule, ...]) -> bool:
    if not include and not exclude:
        return True
    text = normalize_match_text(value) if isinstance(value, str) else ""
    if not text:
        return not include
    return (not include or any(_rule_matches(rule, text) for rule in include)) and not any(
        _rule_matches(rule, text) for rule in exclude
    )


@cache
def _bundled_contract_exclusions() -> tuple[TextRule, ...]:
    # Profile filters can narrow a search. Only the bundled policy can purge stored offers.
    config_path = Path(__file__).resolve().parents[1] / "config" / "config.yaml"
    with config_path.open(encoding="utf-8") as stream:
        rules = yaml.safe_load(stream)["filters"]["contract"]["exclude"]
    return TextFilter(exclude=rules).exclude


def has_excluded_contract_signal(
    title: object | None,
    employment_type: object | None,
    description: object | None,
) -> bool:
    normalized_title = normalize_match_text(title)
    normalized_type = normalize_match_text(employment_type)
    declared = f"{normalized_title} {normalized_type}".strip()
    if any(_rule_matches(rule, declared) for rule in _bundled_contract_exclusions()):
        return True
    if normalized_type in {"franchise", "franchise emploi non salarie"}:
        return True
    if re.search(r"(?:^alt\b|\ben alt\b|\b(?:bts|bachelor|master|mba)\b.*\balt\b)", normalized_title):
        return True
    if re.search(r"\b(?:job|emploi|contrat)\s+etudiant\b", normalized_title):
        return True
    return _description_has_excluded_contract(None if description is None else str(description))


@lru_cache(maxsize=4096)
def _description_has_excluded_contract(description: str | None) -> bool:
    normalized_description = normalize_match_text(description)
    if not normalized_description:
        return False
    if re.search(r"\bcreer votre entreprise\b", normalized_description) and re.search(
        r"\b(?:exercer votre activite de maniere independante|notre reseau de franchise)\b", normalized_description
    ):
        return True
    return any(pattern.search(normalized_description) for pattern in _EXCLUDED_CONTRACT_DESCRIPTION_PATTERNS)


class CompanyExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    names: tuple[TextRule, ...] = ()
    types: tuple[str, ...] = ()


class CompanyFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    include: tuple[TextRule, ...] = ()
    exclude: CompanyExclusions = Field(default_factory=CompanyExclusions)
    _catalog: CompanyCatalog | None = PrivateAttr(None)

    def allows(self, value: object) -> bool:
        return not has_company_type(value, self.exclude.types, self._catalog) and _text_allows(
            value, self.include, self.exclude.names
        )


class SalaryFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_annual_eur: PositiveInt | None = None
    max_annual_eur: PositiveInt | None = None

    @model_validator(mode="after")
    def _ordered_bounds(self) -> SalaryFilter:
        if (
            self.min_annual_eur is not None
            and self.max_annual_eur is not None
            and self.min_annual_eur > self.max_annual_eur
        ):
            raise ValueError("min_annual_eur must be at most max_annual_eur")
        return self

    def allows(self, get: Callable[[str], object]) -> bool:
        if self.min_annual_eur is None and self.max_annual_eur is None:
            return True
        minimum, maximum = salary_bounds(get)
        return (self.min_annual_eur is None or minimum is None or minimum >= self.min_annual_eur) and (
            self.max_annual_eur is None or maximum is None or maximum <= self.max_annual_eur
        )


class OfferFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: TextFilter = Field(default_factory=TextFilter)
    description: TextFilter = Field(default_factory=TextFilter)
    contract: TextFilter = Field(default_factory=TextFilter)
    company: CompanyFilter = Field(default_factory=CompanyFilter)
    remote: StrictBool | None = None
    salary: SalaryFilter = Field(default_factory=SalaryFilter)
    posted_after: date | None = None
    posted_before: date | None = None

    @model_validator(mode="after")
    def _ordered_dates(self) -> OfferFields:
        if self.posted_after is not None and self.posted_before is not None and self.posted_after > self.posted_before:
            raise ValueError("posted_after must be no later than posted_before")
        return self

    def allows(self, get: Callable[[str], object]) -> bool:
        text_fields = (
            (self.title, "title"),
            (self.description, "description"),
            (self.contract, "employment_type"),
            (self.company, "company"),
        )
        if not all(rule.allows(get(key)) for rule, key in text_fields):
            return False
        if self.remote is not None:
            remote = {"true": True, "oui": True, "false": False, "non": False}.get(
                str(get("is_remote")).strip().lower()
            )
            if remote is not None and remote is not self.remote:
                return False
        if self.posted_after is not None or self.posted_before is not None:
            first_seen = get("first_seen_at")
            posted = parse_posted_date(get("date_posted"), now=first_seen if isinstance(first_seen, datetime) else None)
            if posted is not None:
                if self.posted_after is not None and posted.day < self.posted_after:
                    return False
                if self.posted_before is not None and posted.day > self.posted_before:
                    return False
        return self.salary.allows(get)


class ListingFilters(OfferFields):
    by_source: dict[str, OfferFields] = Field(default_factory=dict)

    def _company_filters(self) -> tuple[CompanyFilter, ...]:
        return (self.company, *(rules.company for rules in self.by_source.values()))

    def excluded_company_types(self) -> set[str]:
        return {kind for company in self._company_filters() for kind in company.exclude.types}

    def with_company_catalog(self, catalog: CompanyCatalog) -> ListingFilters:
        if unknown := self.excluded_company_types().difference(catalog.types):
            raise ValueError(f"Unknown company types in the community catalog: {', '.join(sorted(unknown))}")
        filters = self.model_copy(deep=True)
        for company in filters._company_filters():
            company._catalog = catalog
        return filters

    def allows(self, get: Callable[[str], object]) -> bool:
        source = get("source")
        local = self.by_source.get(source) if isinstance(source, str) else None
        return super().allows(get) and (local is None or local.allows(get))
