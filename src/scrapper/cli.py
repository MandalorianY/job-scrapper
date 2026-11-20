from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import structlog
import typer
from rich.console import Console
from rich.table import Table

from scrapper.config import SearchConfig
from scrapper.export import export_jobs
from scrapper.location_scope import location_is_in_scope
from scrapper.logging import DEFAULT_LOG_LEVEL, LogLevel, configure_logging
from scrapper.scrapers import (
    scrape_aggregators,
    scrape_hellowork,
    scrape_jobspy_sources,
    scrape_web_search,
    scrape_welcome_to_the_jungle,
)
from scrapper.storage import JobStore

app = typer.Typer(help="Scrape example role jobs and export deduped CSV/XLSX files.")
console = Console()
log = structlog.get_logger(__name__)
TrackerFetch = Callable[[SearchConfig], Path | None]
TrackerSync = Callable[[Path], Path | None]
ProgressReporter = Callable[[str], None]


@app.command()
def run(
    query: Annotated[
        list[str] | None,
        typer.Option("--query", "-q", help="Search query. Repeat to override the defaults."),
    ] = None,
    locations: Annotated[
        list[str] | None,
        typer.Option("--location", "-l", help="Search location. Repeat to override the configured locations."),
    ] = None,
    db: Annotated[Path | None, typer.Option("--db", help="DuckDB storage path.")] = None,
    export_dir: Annotated[Path | None, typer.Option("--export-dir", help="Folder for CSV and Excel exports.")] = None,
    results_wanted: Annotated[int | None, typer.Option("--results-wanted", min=1)] = None,
    jobspy_hours_old: Annotated[
        int | None,
        typer.Option("--jobspy-hours-old", min=1, help="Force JobSpy to search this many past hours, ignoring incremental state."),
    ] = None,
    custom_pages: Annotated[int | None, typer.Option("--custom-pages", min=1)] = None,
    refresh_known: Annotated[
        bool | None,
        typer.Option("--refresh-known/--no-refresh-known", help="Re-open known custom detail pages instead of skipping them."),
    ] = None,
    skip_custom: Annotated[bool, typer.Option("--skip-custom", help="Only run JobSpy sources.")] = False,
    skip_jobspy: Annotated[bool, typer.Option("--skip-jobspy", help="Only run custom Crawlee sources.")] = False,
    tracker_sync: Annotated[
        bool, typer.Option("--tracker-sync/--no-tracker-sync", help="Fetch and publish the remote tracker.")
    ] = True,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Structured JSONL log path.")] = None,
    log_level: Annotated[LogLevel, typer.Option("--log-level", help="Minimum structured log level.")] = DEFAULT_LOG_LEVEL,
    log_console: Annotated[bool, typer.Option("--log-console", help="Also emit structured JSON logs to stderr.")] = False,
) -> None:
    config = _config_from_cli(
        query,
        locations,
        db,
        export_dir,
        log_file,
        results_wanted,
        jobspy_hours_old,
        custom_pages,
        refresh_known,
    )
    configured_log_file = configure_logging(config.log_file, level=log_level, console=log_console)
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
        log_file=str(configured_log_file) if configured_log_file else None,
    )
    tracker_sync_hooks = _tracker_sync_hooks() if tracker_sync else None
    sync_exported_tracker: TrackerSync | None = None
    if tracker_sync_hooks:
        fetch_remote_tracker, sync_exported_tracker = tracker_sync_hooks
        fetch_remote_tracker(config)
    store = _job_store(config)
    try:
        counts: dict[str, int] = {}
        if not skip_jobspy and not skip_custom:
            console.print(
                "[bold]Running JobSpy and custom/free sources in parallel[/bold] "
                "(separate DuckDB connections)"
            )
            source_counts, source_errors = asyncio.run(
                _run_all_sources_parallel(
                    config,
                    store,
                    progress=lambda message: console.print(message, markup=False),
                )
            )
            counts.update(source_counts)
            for source, error in source_errors.items():
                console.print(f"[red]{source} failed:[/red] {error}")
                log.error("source_failed", source=source, error=error)
            store.close()
            store = _job_store(config)
        elif not skip_jobspy:
            console.print("[bold]Running JobSpy sources[/bold] (Indeed, LinkedIn, Glassdoor, Google Jobs)")
            counts.update(
                scrape_jobspy_sources(
                    config,
                    store,
                    progress=lambda message: console.print(message, markup=False),
                )
            )
        elif not skip_custom:
            console.print(
                "[bold]Running custom/free sources[/bold] "
                "(HelloWork, Welcome to the Jungle, web search, aggregators)"
            )
            custom_counts, custom_errors = asyncio.run(_run_custom_sources(config, store))
            counts.update(custom_counts)
            for source, error in custom_errors.items():
                console.print(f"[red]{source} failed:[/red] {error}")
                log.error("source_failed", source=source, error=error)

        purged = store.purge_excluded_offers(config.exclude_terms)
        if purged:
            counts["excluded_removed"] = purged
        purged_nonmatching = store.purge_nonmatching_offers(config.include_terms, config.excluded_title_patterns)
        if purged_nonmatching:
            counts["nonmatching_removed"] = purged_nonmatching
        purged_companies = store.purge_excluded_companies(config.excluded_companies_by_source)
        if purged_companies:
            counts["excluded_companies_removed"] = purged_companies
        purged_out_of_scope = store.purge_out_of_scope_offers(
            lambda location: location_is_in_scope(location, config.location_scope)
        )
        if purged_out_of_scope:
            counts["out_of_scope_removed"] = purged_out_of_scope
        excel_path, csv_path, raw_csv_path, total = export_jobs(
            store,
            config.export_dir,
            config.export.excel_columns,
            repair_fields_enabled_by_default=config.export.repair_fields_enabled_by_default,
            repair_fields_by_source=config.export.repair_fields_by_source,
            direct_links_enabled_by_source=config.export.direct_links_enabled_by_source,
            include_terms=config.include_terms,
            exclude_terms=config.exclude_terms,
            excluded_title_patterns=config.excluded_title_patterns,
            location_allowed=lambda location: location_is_in_scope(location, config.location_scope),
        )
        synced_excel_path = sync_exported_tracker(excel_path) if sync_exported_tracker else None
        _print_summary(counts, total, excel_path, csv_path, raw_csv_path)
        if synced_excel_path:
            console.print(f"Synced Excel: [green]{synced_excel_path}[/green]")
        log.info(
            "scrape_run_completed",
            counts=counts,
            deduped_total=total,
            excel_path=str(excel_path),
            csv_path=str(csv_path),
            raw_csv_path=str(raw_csv_path),
            synced_excel_path=str(synced_excel_path) if synced_excel_path else None,
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception:
        log.exception("scrape_run_failed", duration_seconds=round(time.monotonic() - started_at, 3))
        raise
    finally:
        structlog.contextvars.clear_contextvars()
        store.close()


def _job_store(config: SearchConfig) -> JobStore:
    return JobStore(
        config.database_path,
        source_priority=config.dedupe_source_priority,
        default_source_priority=config.dedupe_default_source_priority,
        dedupe_primary_fields=config.dedupe_fields.primary,
        dedupe_fallback_fields=config.dedupe_fields.fallback,
        dedupe_description_fields=config.dedupe_fields.description,
    )


def _print_summary(counts: dict[str, int], total: int, excel_path: Path, csv_path: Path, raw_csv_path: Path) -> None:
    table = Table(title="Job search export")
    table.add_column("Source")
    table.add_column("Stored / updated", justify="right")
    for source, count in counts.items():
        table.add_row(source, str(count))
    table.add_section()
    table.add_row("Deduped total", str(total))
    console.print(table)
    console.print(f"Excel: [green]{excel_path}[/green]")
    console.print(f"CSV:   [green]{csv_path}[/green]")
    console.print(f"Raw:   [green]{raw_csv_path}[/green]")


async def _run_custom_sources(config: SearchConfig, store: JobStore) -> tuple[dict[str, int], dict[str, str]]:
    semaphore = asyncio.Semaphore(max(1, config.custom_source_concurrency))
    sources: list[tuple[str, Awaitable[int | dict[str, int]]]] = []
    if config.is_source_enabled(config.hellowork.source):
        sources.append(("hellowork", scrape_hellowork(config, store)))
    if config.is_source_enabled(config.welcome_to_the_jungle.source):
        sources.append(("welcome_to_the_jungle", scrape_welcome_to_the_jungle(config, store)))
    if config.is_source_enabled(config.web_search.source):
        sources.append(("web_search", scrape_web_search(config, store)))
    if config.is_source_enabled(config.aggregators.source):
        sources.append(("aggregators", scrape_aggregators(config, store)))

    async def run_source(source: str, scraper: Awaitable[int | dict[str, int]]) -> tuple[str, int | dict[str, int], str | None]:
        try:
            async with semaphore:
                log.info("custom_source_group_started", source=source)
                return source, await scraper, None
        except Exception as exc:
            log.exception("custom_source_group_failed", source=source)
            return source, 0, str(exc)

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
    jobspy_task = asyncio.to_thread(_run_jobspy_sources_with_own_store, config, progress)
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
        return {source: 0}, {source: str(exc)}
    if isinstance(result, tuple):
        return result
    return result, {}


def _run_jobspy_sources_with_own_store(
    config: SearchConfig,
    progress: ProgressReporter | None = None,
) -> dict[str, int]:
    store = _job_store(config)
    try:
        return scrape_jobspy_sources(config, store, progress=progress)
    finally:
        store.close()


def _tracker_sync_hooks() -> tuple[TrackerFetch, TrackerSync] | None:
    try:
        from scrapper.gdrive_sync import fetch_remote_tracker, sync_exported_tracker
    except ModuleNotFoundError as exc:
        if exc.name != "scrapper.gdrive_sync":
            raise
        return None
    return fetch_remote_tracker, sync_exported_tracker


def _config_from_cli(
    query: list[str] | None,
    locations: list[str] | None,
    db: Path | None,
    export_dir: Path | None,
    log_file: Path | None,
    results_wanted: int | None,
    jobspy_hours_old: int | None,
    custom_pages: int | None,
    refresh_known: bool | None,
) -> SearchConfig:
    config = SearchConfig()
    overrides = {}
    if query is not None:
        overrides["queries"] = tuple(query)
    if locations is not None:
        overrides["locations"] = tuple(locations)
        overrides["location_label"] = " / ".join(locations)
    paths = config.paths.model_copy()
    if db is not None:
        paths = paths.model_copy(update={"database_path": db})
    if export_dir is not None:
        paths = paths.model_copy(update={"export_dir": export_dir})
    if log_file is not None:
        paths = paths.model_copy(update={"log_file": log_file})
    if db is not None or export_dir is not None or log_file is not None:
        overrides["paths"] = paths
    if results_wanted is not None:
        overrides["results_wanted_per_query"] = results_wanted
    if jobspy_hours_old is not None:
        overrides["jobspy_hours_old_override"] = jobspy_hours_old
    if custom_pages is not None:
        overrides["custom_max_pages"] = custom_pages
    if refresh_known is not None:
        overrides["refresh_known_details"] = refresh_known
    return config.model_copy(update=overrides)


if __name__ == "__main__":
    app()
