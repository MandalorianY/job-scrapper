"""Normalize advertised salary and flag listings that need manual review."""

from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Iterator
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd

from scrapper.dates import parse_posted_date
from scrapper.text import clean_text, contains_any_terms

MONEY_RE = re.compile(r"(\d[\d\s\u202f.,]*)")
PAY_AMOUNT = r"(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{4,6})"
TEXT_SALARY_RE = re.compile(
    rf"(?<![\d.,])(?:(?P<minimum>{PAY_AMOUNT})\s*(?:€|euros?|EUR)?\s*(?:à|et|[-–—])\s*)?"
    rf"(?P<maximum>{PAY_AMOUNT})\s*(?:€|euros?|EUR)\s*(?:bruts?\s*)?"
    r"(?P<unit>annuels?|mensuels?|(?:par\s+|/\s*)(?:an|mois))\b",
    re.IGNORECASE,
)
PAY_LABEL_RE = re.compile(r"\b(?:salaires?|rémunération|fixe|salary)\b", re.IGNORECASE)


def _salary_bounds(row: pd.Series) -> tuple[int | None, int | None]:
    minimum = _number(row.get("min_amount"))
    maximum = _number(row.get("max_amount"))
    if minimum or maximum:
        minimum, maximum = _annual_salary(minimum, maximum, row.get("interval"), row.get("currency"))
        return _round_salary(minimum), _round_salary(maximum)

    salary_payload = _parse_salary_payload(row.get("salary"))
    if salary_payload:
        minimum, maximum = _bounds_from_monetary_amount(salary_payload)
        if minimum or maximum:
            return _round_salary(minimum), _round_salary(maximum)

    raw_payload = _load_json(row.get("raw_json"))
    for payload in _salary_payloads_from_raw(raw_payload):
        minimum, maximum = _bounds_from_monetary_amount(payload)
        if minimum or maximum:
            return _round_salary(minimum), _round_salary(maximum)
    amounts = set()
    for field in ("salary", "description"):
        text = row.get(field)
        if not isinstance(text, str):
            continue
        for match in TEXT_SALARY_RE.finditer(text):
            context = text[max(0, match.start() - 55) : match.start()]
            if field == "description" and not PAY_LABEL_RE.search(re.split(r"[.;]", context)[-1]):
                continue
            minimum = _number(match["minimum"] or match["maximum"])
            maximum = _number(match["maximum"])
            unit = "month" if match["unit"].lower().endswith(("mois", "mensuel", "mensuels")) else "year"
            lower, upper = _annual_salary(minimum, maximum, unit, "EUR")
            if lower is not None and upper is not None:
                amounts.add((_round_salary(lower), _round_salary(upper)))
    return amounts.pop() if len(amounts) == 1 else (None, None)


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
    unit = str(unit or "").strip().lower()
    currency = str(currency or "").strip().upper()
    factors = {"month": 12, "monthly": 12, "year": 1, "yearly": 1, "annual": 1, "annually": 1}
    if unit not in factors or currency not in {"", "EUR", "€"}:
        return None, None
    if any(value is not None and (not math.isfinite(value) or value <= 0) for value in (minimum, maximum)):
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
        for value in payload.values():
            yield from _salary_payloads_from_raw(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _salary_payloads_from_raw(item)


def _parse_salary_payload(value: object) -> dict[str, object] | None:
    if isinstance(value, dict):
        return value
    text = str(value or "").strip()
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
    text = str(value or "").strip()
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
