"""Build the localized tracker and carry application state between exports."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Hashable, Sequence
from datetime import UTC, date, datetime, time
from typing import cast
from urllib.parse import urlparse

import pandas as pd

from scrapper.dates import parse_posted_date
from scrapper.export.salary import _review_note, _salary_bounds, _salary_label
from scrapper.source_rules import get_source_rules
from scrapper.text import clean_text, has_excluded_contract_signal, listing_url_key, matches_job_title

HYPERLINK_RE = re.compile(r'^=HYPERLINK\("(?P<url>[^"]+)"(?:,"[^"]*")?\)$', re.IGNORECASE)
LINK_COLUMNS = ("Lien", "Postuler", "Offre source")
STATUS_COLORS = {
    "En Attente": ("#FFF2CC", "#7F6000"),
    "Postulé": ("#E2F0D9", "#375623"),
    "Refusé": ("#FCE4D6", "#9C0006"),
}
STATUS_OPTIONS = tuple(STATUS_COLORS)
DEFAULT_STATUS = STATUS_OPTIONS[0]
TRACKER_WIDTHS = {
    "Postulé": 10,
    "Rejeté": 10,
    "Lien": 72,
    "Statut": 15,
    "Postuler": 12,
    "Offre source": 14,
    "Autres liens": 30,
    "Intitulé": 44,
    "Entreprise": 28,
    "Lieu": 30,
    "Contrat": 18,
    "Télétravail": 14,
    "Salaire min (€ annuel)": 18,
    "Salaire max (€ annuel)": 18,
    "Salaire": 18,
    "Publié le": 14,
    "Sources": 24,
    "Description": 80,
    "À vérifier": 76,
}
LOCALIZED_TRACKER_COLUMNS = (
    "Postulé",
    "Rejeté",
    "Lien",
    "Statut",
    "Postuler",
    "Offre source",
    "Autres liens",
    "Intitulé",
    "Entreprise",
    "Lieu",
    "Contrat",
    "Télétravail",
    "Salaire min (€ annuel)",
    "Salaire max (€ annuel)",
    "Salaire",
    "Publié le",
    "Sources",
    "Source principale",
    "Doublons",
    "Description",
    "URL entreprise",
    "Email",
    "Recherche",
    "Zone recherchée",
    "Score complétude",
    "Vu le",
    "Dernière vue",
    "À vérifier",
)
DEFAULT_LOCALIZED_TRACKER_COLUMNS = (
    "Postulé",
    "Rejeté",
    "Lien",
    "Intitulé",
    "Entreprise",
    "Lieu",
    "Contrat",
    "Télétravail",
    "Salaire min (€ annuel)",
    "Salaire max (€ annuel)",
    "Salaire",
    "Publié le",
    "Sources",
    "Recherche",
)


def _merge_existing_tracker_rows(
    current: pd.DataFrame,
    previous: pd.DataFrame,
    *,
    location_allowed: Callable[[object | None], bool] | None = None,
    location_review: Callable[[object | None], bool] | None = None,
    include_terms: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
    excluded_title_patterns: tuple[str, ...] = (),
    excluded_urls: set[str] | None = None,
    current_link_aliases: dict[str, set[str]] | None = None,
) -> pd.DataFrame:
    if previous.empty:
        return current

    current = current.copy()
    known_current_keys = {key for aliases in (current_link_aliases or {}).values() for key in aliases}
    previous = previous.loc[
        previous.apply(
            _tracker_row_allowed,
            axis=1,
            include_terms=include_terms,
            exclude_terms=exclude_terms,
            location_allowed=location_allowed,
            excluded_title_patterns=excluded_title_patterns,
            excluded_urls=excluded_urls,
            known_current_keys=known_current_keys,
        )
    ]
    previous = cast(
        pd.DataFrame, previous.apply(_normalize_previous_tracker_row, axis=1, location_review=location_review)
    )
    previous_by_key: dict[str, pd.Series] = {}
    for _, row in previous.iterrows():
        key = _tracker_row_key(row)
        if key and key not in previous_by_key:
            previous_by_key[key] = row

    current_keys: set[str] = set()
    for index, row in current.iterrows():
        key = _tracker_row_key(row)
        if not key:
            continue
        aliases = (current_link_aliases or {}).get(key, {key})
        current_keys.update(aliases)
        matching = [
            previous_by_key[alias]
            for alias in sorted(aliases, key=lambda alias: (alias != key, alias))
            if alias in previous_by_key
        ]
        if matching:
            previous_row = max(matching, key=lambda row: STATUS_OPTIONS.index(_normalize_status(row.get("Statut"))))
            _preserve_tracker_state(current, index, previous_row)
        statuses = {_normalize_status(row.get("Statut")) for row in matching}
        if len(statuses) > 1 and "À vérifier" in current.columns:
            _append_review_note(current, index, "Statuts divergents entre doublons : " + " / ".join(sorted(statuses)))

    retained_previous = [
        row for _, row in previous.iterrows() if (key := _tracker_row_key(row)) and key not in current_keys
    ]
    if not retained_previous:
        if "Rejeté" in current.columns:
            current["Rejeté"] = current["Rejeté"].apply(_coerce_checkbox_value)
        return current
    retained = pd.DataFrame(retained_previous).reindex(columns=current.columns)
    merged = pd.concat([current, retained], ignore_index=True)
    if "Rejeté" in merged.columns:
        merged["Rejeté"] = merged["Rejeté"].apply(_coerce_checkbox_value)
    return merged


def _canonical_link_aliases(frame: pd.DataFrame, direct_links: dict[str, bool]) -> dict[str, set[str]]:
    """Recognize historical links to the same canonical offer after its winning source changes."""
    aliases = {}
    for _, row in frame.iterrows():
        key = listing_url_key(_tracker_apply_link(row, direct_links))
        if key:
            links = [str(row.get(field) or "") for field in ("all_source_links", "all_direct_links")]
            aliases[f"url:{key}"] = {f"url:{key}"} | {
                f"url:{alias}" for link in "\n".join(links).splitlines() if (alias := listing_url_key(link))
            }
    return aliases


def _sort_tracker_for_export(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or "Publié le" not in frame.columns:
        return frame
    sorted_frame = frame.copy()
    sorted_frame["__published_sort"] = pd.to_datetime(
        sorted_frame["Publié le"].map(_date_value),
        errors="coerce",
    )
    sorted_frame = sorted_frame.sort_values(
        by="__published_sort",
        ascending=False,
        na_position="last",
        kind="stable",
    )
    return sorted_frame.drop(columns=["__published_sort"]).reset_index(drop=True)


def _tracker_row_allowed(
    row: pd.Series,
    include_terms: tuple[str, ...],
    exclude_terms: tuple[str, ...],
    location_allowed: Callable[[object | None], bool] | None = None,
    excluded_title_patterns: tuple[str, ...] = (),
    excluded_urls: set[str] | None = None,
    known_current_keys: set[str] | None = None,
) -> bool:
    title = row.get("Intitulé")
    if excluded_urls and any(
        listing_url_key(_extract_hyperlink_url(row.get(column))) in excluded_urls for column in LINK_COLUMNS
    ):
        return False
    if exclude_terms and has_excluded_contract_signal(title, row.get("Contrat"), row.get("Description"), exclude_terms):
        return False
    if include_terms and not matches_job_title(title, include_terms, excluded_title_patterns):
        return False
    return (
        location_allowed is None
        or (known_current_keys is not None and _tracker_row_key(row) in known_current_keys)
        or location_allowed(row.get("Lieu"))
    )


def _offer_location(row: pd.Series) -> str | None:
    source = row.get("best_source") or row.get("source")
    return get_source_rules(source).normalize_location(row.get("location"))


def _offer_row_allowed(
    row: pd.Series,
    include_terms: tuple[str, ...],
    exclude_terms: tuple[str, ...],
    excluded_title_patterns: tuple[str, ...],
    location_allowed: Callable[[object | None], bool] | None,
) -> bool:
    if exclude_terms and has_excluded_contract_signal(
        row.get("title"), row.get("employment_type"), row.get("description"), exclude_terms
    ):
        return False
    if include_terms and not matches_job_title(row.get("title"), include_terms, excluded_title_patterns):
        return False
    return location_allowed is None or location_allowed(row.get("location"))


def _normalize_previous_tracker_row(
    row: pd.Series, location_review: Callable[[object | None], bool] | None = None
) -> pd.Series:
    status = row.get("Statut")
    if _is_blank(status) and _coerce_checkbox_value(row.get("Postulé")):
        status = "Postulé"
    normalized = pd.Series({**row.to_dict(), "Statut": _normalize_status(status)}, dtype=object)
    if "Postulé" in normalized.index:
        normalized["Postulé"] = _checkbox_value_from_previous(normalized)
    if "Rejeté" in normalized.index:
        normalized["Rejeté"] = _coerce_checkbox_value(normalized.get("Rejeté"))
    if "Lieu" in normalized.index:
        normalized["Lieu"] = _tracker_town(normalized.get("Lieu"), location_review)
    if "Publié le" in normalized.index:
        normalized["Publié le"] = _date_value(normalized.get("Publié le"))
    for column in ("Lien", "Postuler", "Offre source", "URL entreprise"):
        if column not in normalized.index:
            continue
        url = _extract_hyperlink_url(normalized.get(column))
        if url:
            normalized[column] = _hyperlink(url)
    return normalized


def _preserve_tracker_state(current: pd.DataFrame, index: Hashable, previous: pd.Series) -> None:
    if "Statut" in current.columns:
        current.at[index, "Statut"] = _normalize_status(previous.get("Statut"))
    if "Postulé" in current.columns:
        current.at[index, "Postulé"] = _checkbox_value_from_previous(previous)
    if "Rejeté" in current.columns:
        current.at[index, "Rejeté"] = _coerce_checkbox_value(previous.get("Rejeté"))
    if "À vérifier" in current.columns:
        for note in str(previous.get("À vérifier") or "").split(" ; "):
            if note.startswith("Statuts divergents entre doublons :"):
                _append_review_note(current, index, note)


def _append_review_note(frame: pd.DataFrame, index: Hashable, note: str) -> None:
    value = frame.at[index, "À vérifier"]
    current = "" if _is_blank(value) else str(value)
    if note not in current:
        frame.at[index, "À vérifier"] = " ; ".join(part for part in (current, note) if part)


def _tracker_row_key(row: pd.Series) -> str | None:
    for column in LINK_COLUMNS:
        if column not in row.index:
            continue
        extracted_url = _extract_hyperlink_url(row.get(column))
        key = listing_url_key(extracted_url)
        if key:
            return f"url:{key}"

    title = _normalized_tracker_text(row.get("Intitulé"))
    company = _normalized_tracker_text(row.get("Entreprise"))
    location = _normalized_tracker_text(row.get("Lieu"))
    if title and company:
        return f"text:{title}|{company}|{location}"
    return None


def _extract_hyperlink_url(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    match = HYPERLINK_RE.match(text)
    return match.group("url") if match else text if text.startswith(("http://", "https://")) else None


def _normalized_tracker_text(value: object) -> str:
    return (clean_text(value) or "").casefold()


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return str(value).strip() == ""


def _checkbox_value_from_previous(previous: pd.Series) -> bool:
    status = str(previous.get("Statut") or "").strip()
    if status == "Postulé":
        return True
    return _coerce_checkbox_value(previous.get("Postulé"))


def _coerce_checkbox_value(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, float) and math.isnan(value):
        return False
    text = str(value).strip().casefold()
    return text in {"true", "1", "yes", "oui", "x", "checked"}


def _normalize_status(value: object) -> str:
    text = str(value or "").strip()
    return text if text in STATUS_OPTIONS else DEFAULT_STATUS


def _localized_job_tracker(
    frame: pd.DataFrame,
    repair_fields_enabled_by_default: bool = True,
    repair_fields_by_source: dict[str, bool] | None = None,
    direct_links_enabled_by_source: dict[str, bool] | None = None,
    location_review: Callable[[object | None], bool] | None = None,
) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    repair_fields_by_source = repair_fields_by_source or {}
    direct_links_enabled_by_source = direct_links_enabled_by_source or {}
    for _, row in frame.iterrows():
        salary_min, salary_max = _salary_bounds(row)
        best_source = str(row.get("best_source") or "").strip()
        should_repair = repair_fields_by_source.get(best_source, repair_fields_enabled_by_default)
        source_rules = get_source_rules(best_source)
        if should_repair:
            title, company, location = source_rules.repair_tracker_fields(
                row.get("title"),
                row.get("company"),
                row.get("location"),
                row.get("description"),
            )
        else:
            title = clean_text(row.get("title"))
            company = clean_text(row.get("company"))
            location = clean_text(row.get("location"))
        apply_link = _tracker_apply_link(row, direct_links_enabled_by_source)
        records.append(
            {
                "Postulé": False,
                "Rejeté": False,
                "Lien": _hyperlink(apply_link),
                "Statut": DEFAULT_STATUS,
                "Postuler": _hyperlink(apply_link),
                "Offre source": _hyperlink(row.get("best_source_url")),
                "Autres liens": _compact_links(row.get("all_source_links"), row.get("all_direct_links")),
                "Intitulé": title,
                "Entreprise": company,
                "Lieu": _tracker_town(location, location_review),
                "Contrat": _contract_fr(row.get("employment_type")),
                "Télétravail": _remote_fr(row.get("is_remote")),
                "Salaire min (€ annuel)": salary_min,
                "Salaire max (€ annuel)": salary_max,
                "Salaire": _salary_label(salary_min, salary_max),
                "Publié le": _date_value(row.get("date_posted")),
                "Sources": row.get("sources"),
                "Source principale": row.get("best_source"),
                "Doublons": row.get("duplicate_count"),
                "Description": row.get("description"),
                "URL entreprise": _hyperlink(row.get("company_url")),
                "Email": row.get("emails"),
                "Recherche": clean_text(row.get("search_query")),
                "Zone recherchée": row.get("search_location"),
                "Score complétude": row.get("completeness_score"),
                "Vu le": _date_value(row.get("first_seen_at")),
                "Dernière vue": _date_value(row.get("last_seen_at")),
                "À vérifier": _review_note(row),
            }
        )
    return pd.DataFrame.from_records(records, columns=LOCALIZED_TRACKER_COLUMNS)


def _tracker_apply_link(row: pd.Series, direct_links_enabled_by_source: dict[str, bool]) -> str | None:
    source = str(row.get("best_source") or "").strip()
    direct_url = row.get("best_direct_url")
    if direct_links_enabled_by_source.get(source, True) and isinstance(direct_url, str) and direct_url:
        return direct_url
    source_url = row.get("best_source_url")
    return source_url if isinstance(source_url, str) else None


def _select_tracker_columns(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    if not columns:
        raise ValueError("At least one Excel column must be configured.")
    unknown = tuple(column for column in columns if column not in LOCALIZED_TRACKER_COLUMNS)
    if unknown:
        available = ", ".join(LOCALIZED_TRACKER_COLUMNS)
        requested = ", ".join(unknown)
        raise ValueError(f"Unknown Excel column(s): {requested}. Available columns: {available}")
    return frame.loc[:, list(columns)]


def _hyperlink(url: object, label: str | None = None) -> str:
    text = str(url or "").strip()
    if not text or text.lower() in {"nan", "none"}:
        return ""
    if text.casefold().endswith("#postuler"):
        parsed = urlparse(text)
        if parsed.hostname in {"hellowork.com", "www.hellowork.com"} and parsed.path.startswith("/fr-fr/emplois/"):
            text = parsed._replace(fragment="").geturl()
    safe_url = text.replace('"', '""')
    safe_label = (label or text).replace('"', '""')
    return f'=HYPERLINK("{safe_url}","{safe_label}")'


def _compact_links(*values: object) -> str:
    links: list[str] = []
    for value in values:
        for link in str(value or "").splitlines():
            link = link.strip()
            if link and link.lower() not in {"nan", "none"} and link not in links:
                links.append(link)
    return "\n".join(links[:5])


def _contract_fr(value: object) -> str:
    text = str(value or "").strip()
    mapping = {
        "fulltime": "Temps plein",
        "parttime": "Temps partiel",
        "temporary": "CDD / Temporaire",
        "contract": "Contrat",
        "contractor": "Indépendant",
        "other": "Autre",
    }
    parts = [
        mapping.get(re.sub(r"[\s_-]+", "", part.casefold()), part) for part in map(str.strip, text.split(",")) if part
    ]
    deduped = []
    for part in parts:
        if part and part not in deduped:
            deduped.append(part)
    return ", ".join(deduped)


def _remote_fr(value: object) -> str:
    if value is True or str(value).lower() == "true":
        return "Oui"
    if value is False or str(value).lower() == "false":
        return "Non"
    return ""


def _tracker_town(value: object | None, location_review: Callable[[object | None], bool] | None) -> str:
    if location_review is not None and location_review(value):
        return clean_text(value) or ""
    return _town_fr(value)


def _town_fr(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(r"\s*\(\d{2,3}\s*-\s*[^()]+\)$", "", text)
    department_match = re.search(r"(?:^|\b)\d{2,3}\s*-\s*([^,]+)", text)
    if department_match:
        return _clean_town_name(department_match.group(1))
    parts = [part.strip() for part in text.split(",") if part.strip()]
    if len(parts) > 1 and re.match(r"^\d{1,4}(?:\s*(?:bis|ter))?\s+", parts[0], flags=re.IGNORECASE):
        return _clean_town_name(parts[1])
    return _clean_town_name(parts[0] if parts else text)


def _clean_town_name(value: str) -> str:
    town = clean_text(value) or ""
    town = re.sub(
        r"\s+\d+(?:er|e|eme|ème)?\s+arrondissement\b.*$|\s+\d{1,2}(?:er|e|eme|ème)$", "", town, flags=re.IGNORECASE
    )
    town = re.sub(r"\s+-\s+localiser\b.*$", "", town, flags=re.IGNORECASE)
    town = re.sub(r"^\d{5}\s+|\s*\([^()]*(?:\(\d{2,3}\))\)$|\s*\(\d{2,5}\)$", "", town)
    return town.strip()


def _date_value(value: object, today: date | None = None) -> date | None:
    reference = datetime.combine(today, time(hour=12), tzinfo=UTC) if today else None
    parsed = parse_posted_date(value, now=reference)
    return parsed.day if parsed else None


def _date_fr(value: object, today: date | None = None) -> str:
    parsed = _date_value(value, today=today)
    if parsed:
        return _format_date(parsed)
    text = str(value or "").strip()
    if not text or text.lower() in {"nat", "nan", "none"}:
        return ""
    return clean_text(text) or ""


def _format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
    converted = frame.copy()
    for column in converted.columns:
        if pd.api.types.is_datetime64_any_dtype(converted[column]):
            converted[column] = converted[column].astype(str).replace({"NaT": ""})
        elif pd.api.types.is_string_dtype(converted[column].dtype):
            converted[column] = converted[column].map(_safe_cell)
    return converted


def _safe_cell[CellValue](value: CellValue) -> CellValue | str:
    try:
        if bool(pd.isna(value)):
            return ""
    except (TypeError, ValueError):
        pass
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return cast(str, isoformat())
    return value
