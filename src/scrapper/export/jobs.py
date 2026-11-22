"""Publish the selected job export formats."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from shutil import copy2
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, cast

import pandas as pd
import structlog

from scrapper.export.tracker import (
    DEFAULT_LOCALIZED_TRACKER_COLUMNS,
    _append_review_note,
    _canonical_link_aliases,
    _excel_safe,
    _localized_job_tracker,
    _merge_existing_tracker_rows,
    _offer_location,
    _select_tracker_columns,
    _sort_tracker_for_export,
)
from scrapper.export.xlsx import read_xlsx_tracker, write_xlsx
from scrapper.filters import ListingFilters
from scrapper.text import has_excluded_contract_signal, offer_link_keys

if TYPE_CHECKING:
    from scrapper.config.settings import ExportFormat
    from scrapper.storage import JobStore

log = structlog.get_logger(__name__)

_EXPORT_FILES = {
    "xlsx": ("jobs.xlsx",),
    "ods": ("jobs.ods",),
    "csv": ("jobs.csv", "jobs_raw.csv"),
}


def export_paths(export_dir: Path, formats: Sequence[ExportFormat]) -> tuple[Path, ...]:
    return tuple(export_dir / filename for kind in formats for filename in _EXPORT_FILES[kind])


def tracker_paths(export_dir: Path, formats: Sequence[ExportFormat]) -> tuple[Path, ...]:
    primary, *additional = formats
    workbook_fallbacks = [kind for kind in additional if kind != "csv"]
    return tuple(export_dir / f"jobs.{kind}" for kind in dict.fromkeys((primary, *workbook_fallbacks, "xlsx", "csv")))


def export_job_formats(
    store: JobStore,
    export_dir: Path,
    excel_columns: Sequence[str] | None = None,
    *,
    formats: tuple[ExportFormat, ...],
    repair_fields_enabled_by_default: bool = True,
    repair_fields_by_source: dict[str, bool] | None = None,
    direct_links_enabled_by_source: dict[str, bool] | None = None,
    filters: ListingFilters | None = None,
    location_allowed: Callable[[object | None], bool] | None = None,
    location_review: Callable[[object | None], bool] | None = None,
) -> tuple[tuple[Path, ...], int]:
    if not formats or len(set(formats)) != len(formats):
        raise ValueError("Choose one or more distinct export formats.")
    outputs = export_paths(export_dir, formats)
    export_dir.mkdir(parents=True, exist_ok=True)

    filters = filters or ListingFilters()
    canonical = _excel_safe(
        store.canonical_dataframe(allows=lambda row: filters.allows(row.get), exclude_contracts=True)
    )
    raw = store.raw_dataframe()
    excluded_urls = store.excluded_url_keys()
    out_of_scope_urls = set()
    for _, row in raw.iterrows():
        if has_excluded_contract_signal(row.get("title"), row.get("employment_type"), row.get("description")):
            excluded_urls.update(offer_link_keys(row.get("source_url"), row.get("direct_url")))
        if not filters.allows(row.get) or (location_allowed and not location_allowed(_offer_location(row))):
            out_of_scope_urls.update(offer_link_keys(row.get("source_url"), row.get("direct_url")))
    raw = _excel_safe(raw.drop(columns=["company_description", "raw_json", "currency"]))
    if not canonical.empty:
        canonical["location"] = canonical.apply(_offer_location, axis=1)
        if location_allowed is not None:
            canonical = canonical.loc[cast("pd.Series", canonical["location"].map(location_allowed))]
    localized = _select_tracker_columns(
        _localized_job_tracker(
            canonical,
            repair_fields_enabled_by_default=repair_fields_enabled_by_default,
            repair_fields_by_source=repair_fields_by_source,
            direct_links_enabled_by_source=direct_links_enabled_by_source,
            location_review=location_review,
        ),
        excel_columns or DEFAULT_LOCALIZED_TRACKER_COLUMNS,
    )
    aliases = _canonical_link_aliases(canonical, direct_links_enabled_by_source or {})
    current_link_keys = {alias.removeprefix("url:") for links in aliases.values() for alias in links}
    localized = _merge_existing_tracker_rows(
        localized,
        _read_existing_tracker(tracker_paths(export_dir, formats)),
        location_allowed=location_allowed,
        location_review=location_review,
        filters=filters,
        excluded_urls=excluded_urls | (out_of_scope_urls - current_link_keys),
        current_link_aliases=aliases,
    )
    if location_review is not None and {"À vérifier", "Lieu"} <= set(localized.columns):
        for index, location in localized["Lieu"].items():
            if location_review(location):
                _append_review_note(localized, index, "Localisation à vérifier")
    localized = _sort_tracker_for_export(localized)
    if "Postulé" in localized.columns and "Statut" in localized.columns:
        localized["Postulé"] = localized["Statut"].eq("Postulé")

    with TemporaryDirectory(prefix=".jobs-", dir=export_dir) as staging:
        staged = Path(staging)
        for output in outputs:
            path = staged / output.name
            if output.suffix == ".csv":
                frame = raw if output.name == "jobs_raw.csv" else localized
                frame.to_csv(path, index=False)
            else:
                _write_workbook(path, localized, raw)
        _publish_exports(staged, outputs)

    return outputs, len(localized)


def _write_workbook(path: Path, tracker: pd.DataFrame, raw: pd.DataFrame) -> None:
    if path.suffix == ".ods":
        from scrapper.export.ods import write_ods

        write_ods(path, tracker, raw)
    else:
        write_xlsx(path, tracker, raw)


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


def read_tracker(path: Path) -> pd.DataFrame:
    if path.suffix == ".ods":
        from scrapper.export.ods import read_ods_tracker

        return read_ods_tracker(path)
    if path.suffix == ".xlsx":
        return read_xlsx_tracker(path)
    if path.suffix == ".csv":
        return pd.read_csv(path, dtype=object).fillna("")
    raise ValueError(f"Unsupported tracker format: {path.suffix}")


def _read_existing_tracker(paths: Sequence[Path]) -> pd.DataFrame:
    for path in paths:
        if not path.exists():
            continue
        if path.suffix == ".ods":
            return read_tracker(path)
        try:
            return read_tracker(path)
        except Exception as exc:  # noqa: BLE001 - fall back to a fresh export.
            log.warning("existing_tracker_read_failed", path=str(path), error=str(exc))
    return pd.DataFrame()
