"""Evaluate configured filters against normalized listing fields."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime

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

from scrapper.company_types import CompanyCatalog, has_company_type
from scrapper.dates import parse_posted_date
from scrapper.salary import salary_bounds
from scrapper.text import literal_term_pattern, normalize_match_text


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


class CompanyExclusions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    names: tuple[TextRule, ...] = ()
    types: tuple[str, ...] = ()


class CompanyFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    include: tuple[TextRule, ...] = ()
    exclude: CompanyExclusions | tuple[TextRule, ...] = ()
    _catalog: CompanyCatalog | None = PrivateAttr(None)

    @property
    def excluded_types(self) -> tuple[str, ...]:
        return self.exclude.types if isinstance(self.exclude, CompanyExclusions) else ()

    def allows(self, value: object) -> bool:
        if isinstance(self.exclude, CompanyExclusions):
            return not has_company_type(value, self.exclude.types, self._catalog) and _text_allows(
                value, self.include, self.exclude.names
            )
        return _text_allows(value, self.include, self.exclude)


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
    source: TextFilter = Field(default_factory=TextFilter)
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
            (self.source, "source"),
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
        return {kind for company in self._company_filters() for kind in company.excluded_types}

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
