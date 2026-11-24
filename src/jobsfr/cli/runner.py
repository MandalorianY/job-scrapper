"""Run searches, offline exports, replay, and settings reports."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from importlib.util import find_spec
from pathlib import Path
from typing import Literal

import duckdb
import httpx
import structlog
import typer
import yaml
from pydantic import ValidationError
from pydantic_settings import SettingsError
from rich.text import Text

from jobsfr import CLI_NAME
from jobsfr.cli.output import (
    console,
    print_export_summary,
    print_search_setup_error,
    print_search_status,
    print_source_status,
    print_tracker_sync_status,
)
from jobsfr.config import SearchConfig
from jobsfr.config.settings import SearchSetupRequired, _leaf_values, default_dotenv_path, yaml_config_files
from jobsfr.export import export_job_formats, export_paths
from jobsfr.export.jobs import TRACKER_HISTORY_FILENAME
from jobsfr.geography.regions import load_location_scope, route_location_overrides, save_location_scope
from jobsfr.geography.scope import location_is_allowed, location_needs_review
from jobsfr.runtime.logging import DEFAULT_LOG_LEVEL, LogLevel, configure_logging
from jobsfr.scrapers import (
    scrape_aggregators,
    scrape_jobspy_sources,
    scrape_welcome_to_the_jungle,
)
from jobsfr.scrapers.aggregators import AGGREGATORS_SOURCE
from jobsfr.scrapers.async_http import SourceBlockedError
from jobsfr.scrapers.wttj import WTTJ_SOURCE
from jobsfr.storage import JobStore

log = structlog.get_logger(__name__)
ProgressReporter = Callable[[str], None]


def run(
    *,
    query: list[str] | None = None,
    locations: list[str] | None = None,
    db: Path | None = None,
    export_dir: Path | None = None,
    results_wanted: int | None = None,
    custom_pages: int | None = None,
    export_only: bool = False,
    skip_custom: bool = False,
    skip_jobspy: bool = False,
    replay_from: Path | None = None,
    log_file: Path | None = None,
    profile: Path | None = None,
    log_level: LogLevel = DEFAULT_LOG_LEVEL,
    report: Literal["text", "json"] | None = None,
) -> None:
    if export_only:
        skip_custom = True
        skip_jobspy = True
    config_sources = {} if report is not None else None
    live_search = report is None and replay_from is None and not (skip_jobspy and skip_custom)
    try:
        config = _config_from_cli(
            query,
            locations,
            db,
            export_dir,
            log_file,
            results_wanted,
            custom_pages,
            config_sources,
            profile,
            live_search=live_search,
        )
    except SearchSetupRequired as exc:
        print_search_setup_error(str(exc))
        raise typer.Exit(2) from exc
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        details = "\n".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors)
        raise typer.BadParameter(f"Invalid configuration.\n{details}") from exc
    except SettingsError as exc:
        raise typer.BadParameter(f"Invalid configuration. {exc}") from exc
    except UnicodeDecodeError as exc:
        raise typer.BadParameter("Configuration files must be UTF-8.") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        detail = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise typer.BadParameter(f"Invalid YAML configuration{detail}.") from exc
    except OSError as exc:
        raise typer.BadParameter(f"Could not read configuration: {exc}") from exc
    if config_sources is not None:
        before_scope = dict(_leaf_values(config.location_scope.model_dump()))
        scope = load_location_scope(config, config.database_path, warn=False)
        for key, value in _leaf_values(scope.model_dump()):
            if key not in before_scope or before_scope[key] != value:
                config_sources[".".join(("location_scope", *key))] = "saved geographic scope"
        config = config.model_copy(update={"location_scope": scope})
        settings_report = config.describe(config_sources)
        if report == "json":
            typer.echo(json.dumps(settings_report, ensure_ascii=False, indent=2))
        else:
            print_tracker_sync_status(config)
            print_search_status(config, settings_report["sources"], profile)
            print_source_status(config)
        return
    if "ods" in config.export.formats and find_spec("odf") is None:
        raise typer.BadParameter(f"ODS export requires odfpy. With uv run, add --extra ods before {CLI_NAME}.")
    if export_only and replay_from is None and not config.database_path.is_file():
        raise typer.BadParameter(f"Database file not found: {config.database_path}", param_hint="--db")
    _validate_artifact_paths(config, replay_from, profile)
    try:
        config.export_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise typer.BadParameter(f"Could not open the export folder: {exc}", param_hint="--export-dir") from exc
    try:
        configured_log_file = configure_logging(config.log_file, level=log_level)
    except OSError as exc:
        raise typer.BadParameter(f"Could not open the log file: {exc}", param_hint="--log-file") from exc
    run_id = str(uuid.uuid4())
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        run_id=run_id,
        app_env=config.app_env,
        database_path=str(config.database_path),
        export_dir=str(config.export_dir),
    )
    started_at = time.monotonic()
    log.info(
        "scrape_run_started",
        queries=list(config.queries),
        locations=list(config.search_locations),
        skip_custom=skip_custom,
        skip_jobspy=skip_jobspy,
        refresh_known_details=config.refresh_known_details,
        company_catalog_revision=config.company_catalog_revision,
        log_file=str(configured_log_file) if configured_log_file else None,
    )
    store = None
    try:
        unrouted_config = config
        config = _resolve_run_scope(
            config,
            locations_overridden=locations is not None,
            replay_from=replay_from,
            reroute=live_search,
        )
        if live_search:
            save_location_scope(unrouted_config, config.location_scope)
        elif (
            config.location_scope.enabled
            and config.country.casefold() == "france"
            and config.search_locations
            and not config.location_scope.centers
        ):
            console.print(Text("Radius filter unavailable. Cache these towns with a live search.", style="yellow"))
        store = _job_store(config, run_id)
        location_allowed = (
            (lambda location: location_is_allowed(location, config.location_scope))
            if config.location_scope.centers
            else None
        )
        counts: dict[str, int] = {}
        if replay_from is None and not (skip_jobspy and skip_custom):
            store.prune_observations()
        if replay_from is not None:
            counts["observations_replayed"] = store.replay_observations(replay_from)
        elif not skip_jobspy and not skip_custom:
            console.print(
                "[bold cyan]Running JobSpy and custom sources in parallel[/bold cyan] (separate DuckDB connections)"
            )
            source_counts, source_errors = asyncio.run(
                _run_all_sources_parallel(
                    config,
                    store,
                    progress=_print_jobspy_progress,
                )
            )
            counts.update(source_counts)
            _report_source_errors(source_errors)
            store.close()
            store = None
            store = _job_store(config, run_id)
        elif not skip_jobspy:
            console.print("[bold cyan]Running JobSpy sources[/bold cyan]")
            counts.update(
                scrape_jobspy_sources(
                    config,
                    store,
                    progress=_print_jobspy_progress,
                )
            )
        elif not skip_custom:
            console.print("[bold cyan]Running custom sources[/bold cyan] (Welcome to the Jungle and aggregators)")
            custom_counts, custom_errors = asyncio.run(_run_custom_sources(config, store))
            counts.update(custom_counts)
            _report_source_errors(custom_errors)

        purged = store.purge_excluded_offers()
        if purged:
            counts["excluded_removed"] = purged
        outputs, total = export_job_formats(
            store,
            config.export_dir,
            config.export.excel_columns,
            keep_reposts=config.export.keep_reposts,
            repair_fields_enabled_by_default=config.export.repair_fields_enabled_by_default,
            repair_fields_by_source=config.export.repair_fields_by_source,
            direct_links_enabled_by_source=config.export.direct_links_enabled_by_source,
            filters=config.filters,
            location_allowed=location_allowed,
            location_review=(lambda location: location_needs_review(location, config.location_scope))
            if location_allowed and config.location_scope.include_unrecognized
            else None,
            formats=config.export.formats,
        )
        print_export_summary(counts, total, outputs)
        if replay_from is None and not skip_custom:
            for source in store.sources_with_search_error(SourceBlockedError.message):
                console.print(
                    Text(f"{source} blocked by a verification challenge. Remaining searches skipped.", style="yellow")
                )
                log.warning("source_blocked", source=source, error=SourceBlockedError.message)
        log.info(
            "scrape_run_completed",
            counts=counts,
            deduped_total=total,
            export_paths=[str(path) for path in outputs],
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception as exc:
        log.exception("scrape_run_failed", duration_seconds=round(time.monotonic() - started_at, 3))
        if isinstance(exc, (duckdb.Error, OSError)):
            typer.echo(f"Run failed: {exc}", err=True)
            raise typer.Exit(1) from exc
        raise
    finally:
        structlog.contextvars.clear_contextvars()
        if store is not None:
            store.close()


def _validate_artifact_paths(config: SearchConfig, replay_from: Path | None, profile: Path | None) -> None:
    paths = {
        "Database": config.database_path,
        "Log": config.log_file,
        "Tracker history": config.export_dir / TRACKER_HISTORY_FILENAME,
        **{path.name: path for path in export_paths(config.export_dir, config.export.formats)},
    }
    if config.company_catalog_path is not None:
        paths["Employer catalog"] = config.company_catalog_path
    if replay_from is not None and replay_from.resolve() != config.database_path.resolve():
        paths["Replay archive"] = replay_from
    used: dict[Path, str] = {}
    for label, path in paths.items():
        resolved = path.resolve()
        if resolved in used:
            raise typer.BadParameter(f"{label} and {used[resolved]} point to the same file: {resolved}")
        used[resolved] = label
    for path in (*yaml_config_files(config.app_env, profile), default_dotenv_path()):
        resolved = path.resolve()
        if label := used.get(resolved):
            raise typer.BadParameter(f"{label} points to a configuration file: {resolved}")


def _resolve_run_scope(
    config: SearchConfig, *, locations_overridden: bool, replay_from: Path | None, reroute: bool
) -> SearchConfig:
    if reroute and config.search_locations:
        try:
            if locations_overridden and config.country.casefold() == "france":
                # Board-specific place lists refer to the towns in the saved profile.
                config.jobspy.google_query_places = ()
                config.jobspy.glassdoor_regional_fallback_term = None
                config.jobspy.glassdoor_location_override = None
                config.aggregators.location_terms = ()
                config.aggregators.direct_locations = ()
            return route_location_overrides(config, write_cache=True)
        except (httpx.HTTPError, ValueError) as exc:
            raise typer.BadParameter(
                f"Could not route the requested locations: {exc}", param_hint="--location"
            ) from exc
    scope = load_location_scope(config, replay_from or config.database_path)
    return config.model_copy(update={"location_scope": scope})


def _job_store(config: SearchConfig, run_id: str) -> JobStore:
    return JobStore(
        config.database_path,
        source_priority=config.dedupe_source_priority,
        default_source_priority=config.dedupe_default_source_priority,
        dedupe_primary_fields=config.dedupe_fields.primary,
        dedupe_fallback_fields=config.dedupe_fields.fallback,
        dedupe_description_fields=config.dedupe_fields.description,
        run_id=run_id,
    )


def _print_jobspy_progress(message: str) -> None:
    label, separator, detail = message.partition(": ")
    style = "bold cyan"
    if label.startswith("JobSpy failed "):
        style = "bold red"
    elif label.startswith("JobSpy batch failed ") or ", error=" in detail.rpartition(" (seen=")[2]:
        style = "bold yellow"
    elif label.startswith("JobSpy done "):
        style = "bold green"
    console.print(Text.assemble((label + separator, style), detail))


def _report_source_errors(errors: dict[str, str]) -> None:
    for source, error in errors.items():
        console.print(Text.assemble((f"{source} failed: ", "bold red"), error))
        log.error("source_failed", source=source, error=error)


async def _run_custom_sources(config: SearchConfig, store: JobStore) -> tuple[dict[str, int], dict[str, str]]:
    sources: list[tuple[str, Awaitable[int | dict[str, int]]]] = []
    for source, scrape in (
        (WTTJ_SOURCE, scrape_welcome_to_the_jungle),
        (AGGREGATORS_SOURCE, scrape_aggregators),
    ):
        if config.is_source_enabled(source):
            sources.append((source, scrape(config, store)))

    async def run_source(
        source: str, scraper: Awaitable[int | dict[str, int]]
    ) -> tuple[str, int | dict[str, int], str | None]:
        try:
            log.info("custom_source_group_started", source=source)
            return source, await scraper, None
        except Exception as exc:
            log.exception("custom_source_group_failed", source=source)
            return source, 0, str(exc) or type(exc).__name__

    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    results = await asyncio.gather(*(run_source(source, scraper) for source, scraper in sources))
    for source, result, error in results:
        if error:
            counts[source] = 0
            errors[source] = error
        elif isinstance(result, dict):
            counts.update(result)
            log.info("custom_source_group_completed", source=source, counts=result)
        else:
            counts[source] = result
            log.info("custom_source_group_completed", source=source, stored=result)
    return counts, errors


async def _run_all_sources_parallel(
    config: SearchConfig,
    custom_store: JobStore,
    progress: ProgressReporter | None = None,
) -> tuple[dict[str, int], dict[str, str]]:
    jobspy_task = asyncio.to_thread(_run_jobspy_sources_with_own_store, config, custom_store.run_id, progress)
    custom_task = _run_custom_sources(config, custom_store)
    jobspy_result, custom_result = await asyncio.gather(
        _capture_source_group("jobspy", jobspy_task),
        _capture_source_group("custom", custom_task),
    )

    counts: dict[str, int] = {}
    errors: dict[str, str] = {}
    for source_counts, source_errors in (jobspy_result, custom_result):
        counts.update(source_counts)
        errors.update(source_errors)
    return counts, errors


async def _capture_source_group(
    source: str,
    task: Awaitable[dict[str, int] | tuple[dict[str, int], dict[str, str]]],
) -> tuple[dict[str, int], dict[str, str]]:
    try:
        result = await task
    except Exception as exc:
        log.exception("source_group_failed", source=source)
        return {source: 0}, {source: str(exc) or type(exc).__name__}
    if isinstance(result, tuple):
        return result
    return result, {}


def _run_jobspy_sources_with_own_store(
    config: SearchConfig,
    run_id: str,
    progress: ProgressReporter | None = None,
) -> dict[str, int]:
    store = _job_store(config, run_id)
    try:
        return scrape_jobspy_sources(config, store, progress=progress)
    finally:
        store.close()


def _config_from_cli(
    query: list[str] | None,
    locations: list[str] | None,
    db: Path | None,
    export_dir: Path | None,
    log_file: Path | None,
    results_wanted: int | None,
    custom_pages: int | None,
    config_sources: dict[str, str] | None = None,
    profile: Path | None = None,
    *,
    live_search: bool,
) -> SearchConfig:
    overrides: dict[str, object] = {"_config_sources": config_sources, "_profile": profile}
    if query is not None:
        overrides["queries"] = tuple(query)
    if locations is not None:
        overrides["locations"] = tuple(locations)
        overrides["location_label"] = " / ".join(locations)
    paths = {
        name: path.expanduser().absolute()
        for name, path in (("database_path", db), ("export_dir", export_dir), ("log_file", log_file))
        if path is not None
    }
    if paths:
        overrides["paths"] = paths
    if results_wanted is not None:
        overrides["results_wanted_per_query"] = results_wanted
    if custom_pages is not None:
        overrides["custom_max_pages"] = custom_pages
    return SearchConfig(_refresh_company_catalog=live_search, _require_search_setup=live_search, **overrides)
