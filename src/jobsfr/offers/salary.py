"""Normalize advertised salary and flag listings that need manual review."""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Callable, Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from jobsfr.offers.contracts import is_civic_service
from jobsfr.offers.dates import parse_posted_date
from jobsfr.offers.text import clean_text, contains_any_terms

MONEY_RE = re.compile(r"(-?\d[\d\s\u202f.,]*)")
_EUROS = r"(?:€(?:\s*\(\s*euros?\s*\))?|eur(?:os?)?\b(?:\s*\(\s*€\s*\))?)"
_NUMBER = r"(?:\d{1,3}(?:[ .,]\d{3})+|\d{1,6})(?:[.,]\d{1,2})?"
_PAY_AMOUNT = rf"{_EUROS}?\s*{_NUMBER}(?:\s*k)?\s*{_EUROS}?"
_GROSS = r"brute?s?"
_PAY_GRADE = r"(?:\s*\(\d{1,2}(?:er|e|ème) échelon\))?"
_PAY_RANGE = rf"(?P<first>{_PAY_AMOUNT}){_PAY_GRADE}(?:\s*(?:{_GROSS}\s+)?(?:à|et|to|[-–—/])\s*(?P<last>{_PAY_AMOUNT}){_PAY_GRADE})?"
_UNIT = r"(?:annuel(?:le)?s?|mensuel(?:le)?s?|annually|annual|yearly|monthly|(?:par\s+|/\s*)(?:an|mois|year|month))"
_BOUND = r"(?:[àa] partir de|au moins|from|jusqu['’]?[àa]|au maximum|up to)"
_SUFFIX_SALARY = re.compile(
    rf"(?<![\w.,$£-]){_PAY_RANGE}\s*,?\s*(?:(?:{_GROSS}|fixe)\s*)?(?P<unit>{_UNIT})\b", re.IGNORECASE
)
_PREFIX_SALARY = re.compile(
    rf"\b(?P<unit>{_UNIT})\s*(?:(?:{_GROSS}|est)\b\s*)?"
    r"(?:pour (?:un |une quotité de )?\d{1,3}\s*%\s*)?"
    r"(?:sur \d{1,2} mois\s*(?:selon (?:profil|expérience)\s*)?(?:dans la fourchette\s*)?)?"
    rf":?\s*(?:(?P<bound>{_BOUND})\s+|(?:de|entre)\s+)?{_PAY_RANGE}(?!\w|[.,]\d)",
    re.IGNORECASE,
)
_PAY_LABEL = re.compile(r"\b(?:salaires?|rémunération|salary|salariale|package|solde)\b", re.IGNORECASE)
_BENEFIT = re.compile(
    r"\b(?:tickets?|restaurant|panier|prime|bonus|budget|participation|indemnité|indemnitaire|intéressement|"
    r"transport|estimations?|estimées?|estimés?|estimated)\b",
    re.IGNORECASE,
)
_QUALIFIER = re.compile(r"\s*(?:nets?|estimées?|estimés?|estimated|USD|GBP|CHF)\b|\s*[$£]", re.IGNORECASE)
_CURRENCY = re.compile(_EUROS, re.IGNORECASE)
_VARIABLE = re.compile(r"\b(?:variable|commission)s?\b", re.IGNORECASE)
_FIXED = re.compile(r"\b(?:fixe|base)\b", re.IGNORECASE)
_ADVERTISED_ANNUAL_EQUIVALENT = re.compile(
    rf"\s+sur \d{{1,2}} mois,?\s+soit\s+{_PAY_AMOUNT}\s*(?:{_GROSS}\s*)?annuel(?:le)?\b", re.IGNORECASE
)


def salary_bounds(get: Callable[[str], object]) -> tuple[int | None, int | None]:
    if is_civic_service(get("title")) or is_civic_service(get("employment_type")):
        return None, None
    minimum = _number(get("min_amount"))
    maximum = _number(get("max_amount"))
    # JobSpy exposes Glassdoor percentiles without an employer/estimate distinction.
    if (minimum or maximum) and (get("source") or get("best_source")) != "glassdoor":
        minimum, maximum = _annual_salary(minimum, maximum, get("interval"), get("currency"))
        if minimum is not None or maximum is not None:
            return _round_salary(minimum), _round_salary(maximum)

    salary_payload = _parse_salary_payload(get("salary"))
    if salary_payload:
        minimum, maximum = _bounds_from_monetary_amount(salary_payload)
        if minimum or maximum:
            return _round_salary(minimum), _round_salary(maximum)

    raw_payload = _load_json(get("raw_json") or get("raw"))
    for payload in _salary_payloads_from_raw(raw_payload):
        minimum, maximum = _bounds_from_monetary_amount(payload)
        if minimum or maximum:
            return _round_salary(minimum), _round_salary(maximum)
    if isinstance(raw_payload, dict) and get("source") == "welcome_to_the_jungle":
        minimum, maximum = _annual_salary(
            _number(raw_payload.get("salary_yearly_minimum")),
            _number(raw_payload.get("salary_yearly_maximum")),
            "year",
            raw_payload.get("salary_currency"),
        )
        if minimum is not None or maximum is not None:
            return _round_salary(minimum), _round_salary(maximum)
    # A dedicated salary field outranks free text about the job and its benefits.
    for field in ("salary", "title", "description"):
        amounts = set(_text_salary_bounds(get(field), require_label=field == "description"))
        if amounts:
            return amounts.pop() if len(amounts) == 1 else (None, None)
    return None, None


def _text_salary_bounds(value: object, *, require_label: bool) -> Iterator[tuple[int | None, int | None]]:
    if not isinstance(value, str):
        return
    text = value.replace("\u00a0", " ").replace("\u202f", " ").replace("*", "").replace("\\", "")
    previous_end = -1
    for match in sorted(
        (*_SUFFIX_SALARY.finditer(text), *_PREFIX_SALARY.finditer(text)), key=lambda item: (item.start(), -item.end())
    ):
        if match.start() < previous_end or _QUALIFIER.match(text, match.end()):
            continue
        before = re.split(r"[.!?;\n](?!\d)", text[max(0, match.start() - 95) : match.start()])[-1]
        labels = list(_PAY_LABEL.finditer(before))
        context = before[labels[-1].end() :] if labels else before
        if (require_label and not labels) or _BENEFIT.search(context):
            continue
        if _VARIABLE.search(context) and not _FIXED.search(context):
            continue
        if not _CURRENCY.search(match[0]) or re.search(r"\b(?:nets?|usd|gbp|chf)\b|[$£]", context, re.IGNORECASE):
            continue
        first, last = match["first"], match["last"]
        if not last and re.match(
            rf"\s*(?:{_GROSS}\s*)?(?:à|et|[-–—])\s*{_PAY_AMOUNT}", text[match.end() :], re.IGNORECASE
        ):
            continue
        minimum = _advertised_amount(first, shorthand=bool(last and "k" in last.lower()))
        maximum = _advertised_amount(last) if last else minimum
        bound = (match.groupdict().get("bound") or "").casefold()
        if not bound:
            marker = re.search(rf"({_BOUND})\s*$", context, re.IGNORECASE)
            bound = marker[1].casefold() if marker else ""
        if not last and bound:
            if bound.startswith(("jusqu", "au maximum", "up to")):
                minimum = None
            else:
                maximum = None
        unit = "month" if re.search(r"mois|mensuel|month", match["unit"], re.IGNORECASE) else "year"
        if unit == "month" and _ADVERTISED_ANNUAL_EQUIVALENT.match(text, match.end()):
            continue
        # A bare feed label can mistake an annual salary for a monthly one. Require explicit posting text for outliers.
        if unit == "month" and not require_label and max(minimum or 0, maximum or 0) >= 20000:
            continue
        lower, upper = _annual_salary(minimum, maximum, unit, "EUR")
        if lower is not None or upper is not None:
            previous_end = match.end()
            yield _round_salary(lower), _round_salary(upper)


def _advertised_amount(text: str, *, shorthand: bool = False) -> float:
    value = re.search(_NUMBER, text)
    assert value is not None  # The surrounding salary patterns require a number.
    decimal = re.search(r"[.,]\d{1,2}$", value[0])
    integer = value[0][: decimal.start()] if decimal else value[0]
    amount = float(re.sub(r"[ ,.]+", "", integer) + (decimal[0].replace(",", ".") if decimal else ""))
    scale = "k" in text.casefold() or (shorthand and not _CURRENCY.search(text) and amount < 1000)
    return amount * 1000 if scale else amount


def _bounds_from_monetary_amount(payload: dict[str, object]) -> tuple[float | None, float | None]:
    value = payload.get("value") if isinstance(payload, dict) else None
    if isinstance(value, dict):
        minimum = _number(value.get("minValue"))
        maximum = _number(value.get("maxValue"))
        fixed = _number(value.get("value"))
        unit = value.get("unitText") or payload.get("unitText")
        if fixed and not minimum and not maximum:
            minimum = maximum = fixed
        return _annual_salary(minimum, maximum, unit, payload.get("currency"))
    return None, None


def _annual_salary(
    minimum: float | None, maximum: float | None, unit: object, currency: object
) -> tuple[float | None, float | None]:
    unit = str(unit).strip().lower()
    currency = str(currency).strip().upper()
    if currency in {"NONE", "NAN", "<NA>"}:
        currency = ""
    factors = {
        "month": 12,
        "monthly": 12,
        "mois": 12,
        "mensuel": 12,
        "year": 1,
        "yearly": 1,
        "annual": 1,
        "annually": 1,
        "annuel": 1,
        "an": 1,
    }
    if unit not in factors or currency not in {"", "EUR", "€"}:
        return None, None
    if any(value is not None and (not math.isfinite(value) or value <= 1) for value in (minimum, maximum)):
        return None, None
    if minimum is not None and maximum is not None and minimum > maximum:
        return None, None
    factor = factors[unit]
    return (
        minimum * factor if minimum is not None else None,
        maximum * factor if maximum is not None else None,
    )


def _review_note(row: pd.Series) -> str:
    """Surface weak evidence without turning low pay into a contract classification."""
    notes = []
    if contains_any_terms(
        (row.get("description"),), ("nous recrutons des formateurs", "nous recherchons un formateur")
    ):
        notes.append("Missions de formation : vérifier l'adéquation du poste")
    amount = _number(row.get("min_amount"))
    unit = str(row.get("interval") or "").lower()
    limits = {"monthly": 1000, "month": 1000, "hourly": 6, "hour": 6, "yearly": 12000, "year": 12000}
    if amount is not None and unit in limits and 0 < amount < limits[unit]:
        notes.append("Rémunération faible : vérifier stage/alternance ou temps partiel")
    if not clean_text(row.get("description")) and not clean_text(row.get("employment_type")):
        notes.append("Description et type de contrat indisponibles")
    raw = _load_json(row.get("raw_json"))
    posting = raw.get("json_ld") if isinstance(raw, dict) else None
    expiry = posting.get("validThrough") if isinstance(posting, dict) else None
    if isinstance(expiry, str) and re.match(r"^\d{4}-\d{2}-\d{2}(?:$|T)", expiry):
        parsed = parse_posted_date(expiry)
        zone = ZoneInfo("Europe/Paris")
        if parsed:
            deadline = parsed.moment.astimezone(zone).date() if parsed.moment else parsed.day
            now = datetime.now(zone)
            expired = parsed.moment < now if parsed.moment else deadline < now.date()
            if expired:
                notes.append(f"Date limite dépassée ({deadline:%d/%m/%Y})")
    return " ; ".join(notes)


def _salary_payloads_from_raw(payload: object) -> Iterator[dict[str, object]]:
    if isinstance(payload, dict):
        if payload.get("@type") == "MonetaryAmount":
            yield payload
        json_ld = payload.get("json_ld")
        base_salary = json_ld.get("baseSalary") if isinstance(json_ld, dict) else None
        if isinstance(base_salary, dict) and base_salary:
            yield base_salary
        for key, value in payload.items():
            if key != "estimatedSalary":
                yield from _salary_payloads_from_raw(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _salary_payloads_from_raw(item)


def _parse_salary_payload(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    text = str(value).strip() if value is not None else ""
    if not text or text.lower() in {"nan", "none"}:
        return None
    if "MonetaryAmount" in text:
        try:
            parsed = ast.literal_eval(text)
            return parsed if isinstance(parsed, dict) else None
        except (SyntaxError, ValueError):
            return None
    return None


def _load_json(value: object) -> dict[str, object] | list[object] | None:
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        payload: object = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, (dict, list)) else None


def _number(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace("\u202f", " ").replace("\u00a0", " ").replace(",", ".")
    match = MONEY_RE.search(text)
    if not match:
        return None
    compact = match.group(1).replace(" ", "")
    try:
        return float(compact)
    except ValueError:
        return None


def _round_salary(value: float | None) -> int | None:
    if value is None:
        return None
    return round(value)


def _salary_label(minimum: int | None, maximum: int | None) -> str:
    if not minimum and not maximum:
        return ""
    if minimum and maximum and minimum != maximum:
        return f"{_format_int(minimum)}-{_format_int(maximum)} €"
    return f"{_format_int(minimum or maximum)} €"


def _format_int(value: int | None) -> str:
    return "" if value is None else f"{value:,}".replace(",", " ")
