from __future__ import annotations

import ast
import json
import math
import re
from collections.abc import Callable, Hashable, Iterator, Mapping, Sequence
from datetime import UTC, date, datetime, time
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from typing import cast
from urllib.parse import urlparse

import pandas as pd
import structlog
from openpyxl import load_workbook
from xlsxwriter.format import Format
from xlsxwriter.utility import xl_rowcol_to_cell
from xlsxwriter.worksheet import Worksheet

from scrapper.dates import parse_posted_date
from scrapper.source_rules import get_source_rules
from scrapper.source_rules.meteojob import is_placeholder_offer
from scrapper.storage import JobStore
from scrapper.text import clean_text, contains_any_terms, has_excluded_contract_signal, matches_job_title, url_key

log = structlog.get_logger(__name__)

MONEY_RE = re.compile(r"(\d[\d\s\u202f.,]*)")
HYPERLINK_RE = re.compile(r'^=HYPERLINK\("(?P<url>[^"]+)"(?:,"[^"]*")?\)$', re.IGNORECASE)
LINK_COLUMNS = ("Lien", "Postuler", "Offre source")
STATUS_OPTIONS = ("En Attente", "Postulé", "Refusé")
DEFAULT_STATUS = STATUS_OPTIONS[0]
MAX_EXCEL_ROW_INDEX = 1_048_575
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


def export_jobs(
    store: JobStore,
    export_dir: Path,
    excel_columns: Sequence[str] | None = None,
    repair_fields_enabled_by_default: bool = True,
    repair_fields_by_source: dict[str, bool] | None = None,
    direct_links_enabled_by_source: dict[str, bool] | None = None,
    include_terms: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
    location_allowed: Callable[[object | None], bool] | None = None,
    excluded_title_patterns: tuple[str, ...] = (),
) -> tuple[Path, Path, Path, int]:
    export_dir.mkdir(parents=True, exist_ok=True)
    csv_path = export_dir / "jobs.csv"
    raw_csv_path = export_dir / "jobs_raw.csv"
    excel_path = export_dir / "jobs.xlsx"

    canonical = _excel_safe(store.canonical_dataframe())
    raw = _excel_safe(store.raw_dataframe().drop(columns=["company_description", "raw_json", "currency"]))
    excluded_urls = store.excluded_url_keys()
    for _, row in raw.iterrows():
        if not _offer_row_allowed(row, include_terms, exclude_terms, excluded_title_patterns, location_allowed):
            excluded_urls.update(key for field in ("source_url", "direct_url") if (key := url_key(row.get(field))))
    if not canonical.empty:
        allowed = cast(
            "pd.Series",
            canonical.apply(
                _offer_row_allowed,
                axis=1,
                include_terms=include_terms,
                exclude_terms=exclude_terms,
                excluded_title_patterns=excluded_title_patterns,
                location_allowed=location_allowed,
            ),
        )
        canonical = canonical.loc[allowed]
        urls_allowed = cast(
            "pd.Series",
            canonical.apply(
                lambda row: (
                    not any(
                        url_key(link) in excluded_urls
                        for field in ("all_source_links", "all_direct_links")
                        for link in str(row.get(field) or "").splitlines()
                    )
                ),
                axis=1,
            ).astype(bool),
        )
        canonical = canonical.loc[urls_allowed]
    localized = _select_tracker_columns(
        _localized_job_tracker(
            canonical,
            repair_fields_enabled_by_default=repair_fields_enabled_by_default,
            repair_fields_by_source=repair_fields_by_source,
            direct_links_enabled_by_source=direct_links_enabled_by_source,
        ),
        excel_columns or DEFAULT_LOCALIZED_TRACKER_COLUMNS,
    )
    localized = _merge_existing_tracker_rows(
        localized,
        excel_path,
        csv_path,
        location_allowed=location_allowed,
        include_terms=include_terms,
        exclude_terms=exclude_terms,
        excluded_title_patterns=excluded_title_patterns,
        excluded_urls=excluded_urls,
        current_link_aliases=_canonical_link_aliases(canonical, direct_links_enabled_by_source or {}),
    )
    localized = _sort_tracker_for_export(localized)
    if "Postulé" in localized.columns and "Statut" in localized.columns:
        localized["Postulé"] = localized["Statut"].eq("Postulé")

    with TemporaryDirectory(prefix=".jobs-", dir=export_dir) as staging:
        staged = Path(staging)
        localized.to_csv(staged / csv_path.name, index=False)
        raw.to_csv(staged / raw_csv_path.name, index=False)
        with pd.ExcelWriter(
            staged / excel_path.name, engine="xlsxwriter", date_format="dd/mm/yyyy", datetime_format="dd/mm/yyyy"
        ) as writer:
            localized.to_excel(writer, index=False, sheet_name="Offres")
            raw.to_excel(writer, index=False, sheet_name="Données brutes")
            _format_jobs_sheet(writer, "Offres", localized)
            _format_raw_sheet(writer, "Données brutes", raw)
        _publish_exports(staged, (csv_path, raw_csv_path, excel_path))

    return excel_path, csv_path, raw_csv_path, len(localized)


def _publish_exports(staged: Path, outputs: tuple[Path, ...]) -> None:
    # shortcut: replacements are atomic per file. Use versioned manifests if readers need a consistent set during a crash.
    backups = staged / "previous"
    backups.mkdir()
    for output in outputs:
        if output.exists():
            copy2(output, backups / output.name)
    published = []
    try:
        for output in outputs:
            (staged / output.name).replace(output)
            published.append(output)
    except OSError:
        for output in reversed(published):
            backup = backups / output.name
            if backup.exists():
                backup.replace(output)
            else:
                output.unlink()
        raise


def _merge_existing_tracker_rows(
    current: pd.DataFrame,
    excel_path: Path,
    csv_path: Path,
    *,
    location_allowed: Callable[[object | None], bool] | None = None,
    include_terms: tuple[str, ...] = (),
    exclude_terms: tuple[str, ...] = (),
    excluded_title_patterns: tuple[str, ...] = (),
    excluded_urls: set[str] | None = None,
    current_link_aliases: dict[str, set[str]] | None = None,
) -> pd.DataFrame:
    previous = _read_existing_tracker(excel_path, csv_path)
    if previous.empty:
        return current

    current = current.copy()
    previous = previous[
        previous.apply(
            _tracker_row_allowed,
            axis=1,
            include_terms=include_terms,
            exclude_terms=exclude_terms,
            location_allowed=location_allowed,
            excluded_title_patterns=excluded_title_patterns,
            excluded_urls=excluded_urls,
        )
    ]
    previous = previous.apply(_normalize_previous_tracker_row, axis=1)
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
        previous_row = previous_by_key.get(key)
        if previous_row is None:
            previous_row = next((previous_by_key[alias] for alias in sorted(aliases) if alias in previous_by_key), None)
        if previous_row is not None:
            _preserve_tracker_state(current, index, previous_row)
        statuses = {
            _normalize_status(previous_by_key[alias].get("Statut")) for alias in aliases if alias in previous_by_key
        }
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
        key = url_key(_tracker_apply_link(row, direct_links))
        if key:
            links = [str(row.get(field) or "") for field in ("all_source_links", "all_direct_links")]
            aliases[f"url:{key}"] = {f"url:{key}"} | {
                f"url:{alias}" for link in "\n".join(links).splitlines() if (alias := url_key(link))
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
) -> bool:
    if _is_meteojob_placeholder_tracker_row(row):
        return False
    title = row.get("Intitulé")
    if excluded_urls and any(
        url_key(_extract_hyperlink_url(row.get(column))) in excluded_urls for column in LINK_COLUMNS
    ):
        return False
    if exclude_terms and has_excluded_contract_signal(title, row.get("Contrat"), row.get("Description"), exclude_terms):
        return False
    if include_terms and not matches_job_title(title, include_terms, excluded_title_patterns):
        return False
    return location_allowed is None or location_allowed(row.get("Lieu"))


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


def _is_meteojob_placeholder_tracker_row(row: pd.Series) -> bool:
    for column in LINK_COLUMNS:
        if column not in row.index:
            continue
        url = _extract_hyperlink_url(row.get(column))
        if not url or "meteojob.com" not in urlparse(url).netloc.lower():
            continue
        return is_placeholder_offer(
            row.get("Intitulé"),
            row.get("Entreprise"),
            row.get("Publié le"),
            has_jobposting=False,
        )
    return False


def _normalize_previous_tracker_row(row: pd.Series) -> pd.Series:
    normalized = row.copy()
    status = normalized.get("Statut")
    legacy_status = normalized.get("Postulé")
    if _is_blank(status):
        # Older trackers stored the status dropdown or a standalone checkbox under "Postulé".
        if isinstance(legacy_status, str) and legacy_status.strip() in STATUS_OPTIONS:
            status = legacy_status
        elif _coerce_checkbox_value(legacy_status):
            status = "Postulé"
    normalized["Statut"] = _normalize_status(status)
    if "Postulé" in normalized.index:
        normalized["Postulé"] = _checkbox_value_from_previous(normalized)
    if "Rejeté" in normalized.index:
        normalized["Rejeté"] = _coerce_checkbox_value(normalized.get("Rejeté"))
    if "Lieu" in normalized.index:
        normalized["Lieu"] = _town_fr(normalized.get("Lieu"))
    if "Publié le" in normalized.index:
        normalized["Publié le"] = _date_value(normalized.get("Publié le"))
    for column in ("Lien", "Postuler", "Offre source", "URL entreprise"):
        if column not in normalized.index:
            continue
        url = _extract_hyperlink_url(normalized.get(column))
        if url:
            normalized[column] = _hyperlink(url)
    return normalized


def _read_existing_tracker(excel_path: Path, csv_path: Path) -> pd.DataFrame:
    if excel_path.exists():
        try:
            return _read_existing_excel_tracker(excel_path)
        except Exception as exc:  # noqa: BLE001 - a corrupt/open workbook should not block a fresh export.
            log.warning("existing_excel_tracker_read_failed", path=str(excel_path), error=str(exc))
    if csv_path.exists():
        try:
            return pd.read_csv(csv_path, dtype=object).fillna("")
        except Exception as exc:  # noqa: BLE001 - fall back to a fresh export.
            log.warning("existing_csv_tracker_read_failed", path=str(csv_path), error=str(exc))
    return pd.DataFrame()


def _read_existing_excel_tracker(path: Path) -> pd.DataFrame:
    workbook = load_workbook(path, data_only=False, read_only=True)
    try:
        if "Offres" not in workbook.sheetnames:
            return pd.DataFrame()
        worksheet = workbook["Offres"]
        rows = worksheet.iter_rows(values_only=True)
        headers = [str(value).strip() if value is not None else "" for value in next(rows, ())]
        headers = [header for header in headers if header]
        if not headers:
            return pd.DataFrame()
        records = []
        for values in rows:
            if not values or all(value is None for value in values):
                continue
            records.append(
                {header: values[index] if index < len(values) else "" for index, header in enumerate(headers)}
            )
        return pd.DataFrame.from_records(records).fillna("")
    finally:
        workbook.close()


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
    current = str(frame.at[index, "À vérifier"] or "")
    if note not in current:
        frame.at[index, "À vérifier"] = " ; ".join(part for part in (current, note) if part)


def _tracker_row_key(row: pd.Series) -> str | None:
    for column in LINK_COLUMNS:
        if column not in row.index:
            continue
        extracted_url = _extract_hyperlink_url(row.get(column))
        key = url_key(extracted_url) if extracted_url else None
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
    return text in {"true", "1", "yes", "oui", "x", "checked", "postulé", "postule"}


def _normalize_status(value: object) -> str:
    text = str(value or "").strip()
    folded = text.casefold()
    if folded in {"à étudier", "a etudier", "en attente", "attente", ""}:
        return DEFAULT_STATUS
    if folded in {"postulé", "postule"}:
        return "Postulé"
    if folded in {"refusé", "refuse", "rejeté", "rejete"}:
        return "Refusé"
    return text if text in STATUS_OPTIONS else DEFAULT_STATUS


def _localized_job_tracker(
    frame: pd.DataFrame,
    repair_fields_enabled_by_default: bool = True,
    repair_fields_by_source: dict[str, bool] | None = None,
    direct_links_enabled_by_source: dict[str, bool] | None = None,
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
                "Lieu": _town_fr(location),
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
    if direct_links_enabled_by_source.get(source, True) is False:
        url = row.get("best_source_url")
    else:
        url = row.get("best_direct_url") or row.get("best_source_url")
    return url if isinstance(url, str) else None


def _select_tracker_columns(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    if not columns:
        raise ValueError("At least one Excel column must be configured.")
    unknown = tuple(column for column in columns if column not in LOCALIZED_TRACKER_COLUMNS)
    if unknown:
        available = ", ".join(LOCALIZED_TRACKER_COLUMNS)
        requested = ", ".join(unknown)
        raise ValueError(f"Unknown Excel column(s): {requested}. Available columns: {available}")
    return frame.loc[:, list(columns)]


def _format_jobs_sheet(writer: pd.ExcelWriter, sheet_name: str, frame: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    worksheet.freeze_panes(1, 0)
    worksheet.hide_gridlines(2)

    header_format = workbook.add_format(
        {
            "bold": True,
            "font_color": "#FFFFFF",
            "bg_color": "#1F4E78",
            "align": "center",
            "valign": "vcenter",
            "text_wrap": True,
        }
    )
    checkbox_format = workbook.add_format({"align": "center", "valign": "vcenter", "checkbox": True})
    description_format = workbook.add_format({"text_wrap": True, "valign": "top"})
    salary_format = workbook.add_format({"num_format": '# ##0" €"'})
    date_format = workbook.add_format({"num_format": "dd/mm/yyyy"})
    worksheet.set_row(0, 28, header_format)
    for index, column in enumerate(frame.columns):
        worksheet.write(0, index, column, header_format)

    checkbox_col = _optional_column_index(frame, "Postulé")
    rejected_checkbox_col = _optional_column_index(frame, "Rejeté")
    salary_min_col = _optional_column_index(frame, "Salaire min (€ annuel)")
    salary_max_col = _optional_column_index(frame, "Salaire max (€ annuel)")
    description_col = _optional_column_index(frame, "Description")
    status_col = _optional_column_index(frame, "Statut")

    for row in range(1, len(frame) + 1):
        if checkbox_col:
            value = _coerce_checkbox_value(frame.iloc[row - 1, checkbox_col - 1])
            if status_col:
                status_cell = xl_rowcol_to_cell(row, status_col - 1)
                worksheet.write_formula(row, checkbox_col - 1, f'={status_cell}="Postulé"', checkbox_format, value)
            else:
                worksheet.insert_checkbox(row, checkbox_col - 1, value, checkbox_format)
        if rejected_checkbox_col:
            value = _coerce_checkbox_value(frame.iloc[row - 1, rejected_checkbox_col - 1])
            worksheet.insert_checkbox(row, rejected_checkbox_col - 1, value, checkbox_format)
    if status_col:
        worksheet.data_validation(
            1,
            status_col - 1,
            MAX_EXCEL_ROW_INDEX,
            status_col - 1,
            {
                "validate": "list",
                "source": list(STATUS_OPTIONS),
                "input_title": "Statut",
                "input_message": "Choisir un statut.",
                "error_title": "Statut invalide",
                "error_message": "Choisissez En Attente, Postulé ou Refusé.",
            },
        )
        if len(frame):
            for status, background, foreground in (
                ("En Attente", "#FFF2CC", "#7F6000"),
                ("Postulé", "#E2F0D9", "#375623"),
                ("Refusé", "#FCE4D6", "#9C0006"),
            ):
                worksheet.conditional_format(
                    1,
                    status_col - 1,
                    len(frame),
                    status_col - 1,
                    {
                        "type": "cell",
                        "criteria": "==",
                        "value": f'"{status}"',
                        "format": workbook.add_format({"bg_color": background, "font_color": foreground}),
                    },
                )

    widths = {
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
    column_formats = {}
    if checkbox_col:
        column_formats["Postulé"] = checkbox_format
    if rejected_checkbox_col:
        column_formats["Rejeté"] = checkbox_format
    if salary_min_col:
        column_formats["Salaire min (€ annuel)"] = salary_format
    if salary_max_col:
        column_formats["Salaire max (€ annuel)"] = salary_format
    if description_col:
        column_formats["Description"] = description_format
    for column in ("Publié le", "Vu le", "Dernière vue"):
        if column in frame.columns:
            column_formats[column] = date_format
    _apply_widths(worksheet, frame, widths, column_formats)
    _add_table(worksheet, "TableOffres", frame)


def _format_raw_sheet(writer: pd.ExcelWriter, sheet_name: str, frame: pd.DataFrame) -> None:
    worksheet = writer.sheets[sheet_name]
    workbook = writer.book
    header_format = workbook.add_format({"bold": True, "font_color": "#FFFFFF", "bg_color": "#595959"})
    worksheet.freeze_panes(1, 0)
    worksheet.set_row(0, None, header_format)
    for index, column in enumerate(frame.columns):
        worksheet.write(0, index, column, header_format)
    _autosize(worksheet, frame)
    _add_table(worksheet, "TableDonneesBrutes", frame)


def _add_table(worksheet: Worksheet, name: str, frame: pd.DataFrame) -> None:
    row_count = len(frame)
    column_count = len(frame.columns)
    if row_count < 1 or column_count < 1:
        return
    worksheet.add_table(
        0,
        0,
        row_count,
        column_count - 1,
        {
            "name": name,
            "style": "Table Style Medium 2",
            "first_column": False,
            "last_column": False,
            "banded_rows": True,
            "banded_columns": False,
            "autofilter": True,
            "columns": [{"header": column} for column in frame.columns],
        },
    )


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
    return None, None


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
    text = str(value).replace("\u202f", " ").replace(",", ".")
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
        "FULL_TIME": "Temps plein",
        "fulltime": "Temps plein",
        "TEMPORARY": "CDD / Temporaire",
        "temporary": "CDD / Temporaire",
        "CONTRACT": "Contrat",
        "contract": "Contrat",
        "CDI": "CDI",
        "CDD / Temporaire": "CDD / Temporaire",
    }
    parts = [mapping.get(part.strip(), part.strip()) for part in text.split(",") if part.strip()]
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


def _town_fr(value: object) -> str:
    text = clean_text(value)
    if not text:
        return ""
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
    town = re.sub(r"\s*\([^()]*(?:\(\d{2,3}\))\)$|\s*\(\d{2,5}\)$", "", town)
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
        elif converted[column].dtype == "object":
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


def _column_index(frame: pd.DataFrame, column: str) -> int:
    return list(frame.columns).index(column) + 1


def _optional_column_index(frame: pd.DataFrame, column: str) -> int | None:
    if column not in frame.columns:
        return None
    return _column_index(frame, column)


def _apply_widths(
    worksheet: Worksheet, frame: pd.DataFrame, widths: Mapping[str, int], formats: Mapping[str, Format] | None = None
) -> None:
    formats = formats or {}
    for index, column in enumerate(frame.columns):
        width = widths.get(column)
        if not width:
            values = [str(column), *[str(_safe_cell(value)) for value in frame[column].head(100)]]
            width = min(max(len(value) for value in values) + 2, 50)
        worksheet.set_column(index, index, width, formats.get(column))


def _autosize(worksheet: Worksheet, frame: pd.DataFrame) -> None:
    for index, column in enumerate(frame.columns):
        values = [str(column), *[str(_safe_cell(value)) for value in frame[column].head(100)]]
        width = min(max(len(value) for value in values) + 2, 80)
        worksheet.set_column(index, index, width)
