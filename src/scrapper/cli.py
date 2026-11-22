from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from importlib.util import find_spec
from pathlib import Path
from typing import Annotated

import duckdb
import httpx
import structlog
import typer
import yaml
from pydantic import ValidationError
from rich.text import Text

from scrapper import __version__
from scrapper.cli_output import (
    CONFIG_OVERRIDE_HELP,
    console,
    print_export_summary,
    print_search_status,
    print_source_status,
    print_tracker_sync_status,
)
from scrapper.cli_paths import existing_file
from scrapper.config import SearchConfig
from scrapper.config.settings import _leaf_values
from scrapper.export import export_job_formats, export_paths
from scrapper.location_scope import location_is_allowed, location_needs_review
from scrapper.logging import DEFAULT_LOG_LEVEL, LogLevel, configure_logging
from scrapper.scrapers import (
    scrape_aggregators,
    scrape_jobspy_sources,
    scrape_welcome_to_the_jungle,
)
from scrapper.scrapers.aggregators import AGGREGATORS_SOURCE
from scrapper.scrapers.async_http import SourceBlockedError
from scrapper.scrapers.wttj import WTTJ_SOURCE
from scrapper.search_regions import load_location_scope, route_location_overrides, save_location_scope
from scrapper.storage import JobStore

app = typer.Typer(pretty_exceptions_show_locals=False)
log = structlog.get_logger(__name__)
ProgressReporter = Callable[[str], None]


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"job-scrapper {__version__}")
        raise typer.Exit()


@app.command(
    help="[bold cyan]Scrape example role jobs and export deduped XLSX, ODS, or CSV files.[/bold cyan]",
    epilog=f"{CONFIG_OVERRIDE_HELP} Run job-scrapper sync --help for tracker sync.",
)
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
    export_dir: Annotated[
        Path | None, typer.Option("--export-dir", help="Folder for the selected export files.")
    ] = None,
    results_wanted: Annotated[int | None, typer.Option("--results-wanted", min=1)] = None,
    custom_pages: Annotated[int | None, typer.Option("--custom-pages", min=1)] = None,
    export_only: Annotated[
        bool, typer.Option("--export-only", help="Regenerate local exports from DuckDB without scraping.")
    ] = False,
    skip_custom: Annotated[bool, typer.Option("--skip-custom", help="Only run JobSpy sources.")] = False,
    skip_jobspy: Annotated[bool, typer.Option("--skip-jobspy", help="Only run custom sources.")] = False,
    replay_from: Annotated[
        Path | None,
        typer.Option(parser=existing_file, metavar="FILE", help="Rebuild from a DuckDB observation archive offline."),
    ] = None,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Structured JSONL log path.")] = None,
    profile: Annotated[
        Path | None,
        typer.Option(
            "--profile", parser=existing_file, metavar="FILE", help="Use this YAML in place of config.local.yaml."
        ),
    ] = None,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Minimum structured log level.")
    ] = DEFAULT_LOG_LEVEL,
    show_config: Annotated[
        bool, typer.Option("--show-config", help="Print effective settings and their sources offline.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Show search settings, job sources, and tracker sync status offline.")
    ] = False,
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True, help="Show the installed version.")
    ] = False,
) -> None:
    if export_only:
        skip_custom = True
        skip_jobspy = True
    config_sources = {} if show_config or status else None
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
        )
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        details = "\n".join(f"{'.'.join(map(str, error['loc']))}: {error['msg']}" for error in errors)
        raise typer.BadParameter(f"Invalid configuration.\n{details}") from exc
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        detail = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise typer.BadParameter(f"Invalid YAML configuration{detail}.") from exc
    except OSError as exc:
        raise typer.BadParameter(f"Could not read configuration: {exc}") from exc
    live_search = replay_from is None and not (skip_jobspy and skip_custom)
    if live_search and not config.search_locations and config_sources is None:
        raise typer.BadParameter("Set locations in your search config or pass --location.", param_hint="--location")
    if config_sources is not None:
        before_routing = dict(_leaf_values(config.model_dump()))
        config, scope_origin = _resolve_run_scope(
            config,
            locations_overridden=locations is not None,
            replay_from=replay_from,
            reroute=live_search and show_config,
            preview=True,
        )
        for key, value in _leaf_values(config.model_dump()):
            if key not in before_routing or before_routing[key] != value:
                config_sources[".".join(key)] = scope_origin
        report = config.describe(config_sources)
        if show_config:
            typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_tracker_sync_status(config)
            print_search_status(config, report["sources"], profile)
            print_source_status(
                config,
                skip_jobspy=skip_jobspy or replay_from is not None,
                skip_custom=skip_custom or replay_from is not None,
            )
        return
    if "ods" in config.export.formats and find_spec("odf") is None:
        raise typer.BadParameter("ODS export requires odfpy. Run with uv run --locked --extra ods job-scrapper.")
    if export_only and replay_from is None and not config.database_path.is_file():
        raise typer.BadParameter(f"Database file not found: {config.database_path}", param_hint="--db")
    _validate_artifact_paths(config, replay_from)
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
        log_file=str(configured_log_file) if configured_log_file else None,
    )
    store = None
    try:
        unrouted_config = config
        config, _ = _resolve_run_scope(
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

        purged = store.purge_excluded_offers(config.exclude_terms)
        if purged:
            counts["excluded_removed"] = purged
        purged_nonmatching = store.purge_nonmatching_offers(config.include_terms, config.excluded_title_patterns)
        if purged_nonmatching:
            counts["nonmatching_removed"] = purged_nonmatching
        purged_companies = store.purge_excluded_companies(config.excluded_companies_by_source)
        if purged_companies:
            counts["excluded_companies_removed"] = purged_companies
        outputs, total = export_job_formats(
            store,
            config.export_dir,
            config.export.excel_columns,
            repair_fields_enabled_by_default=config.export.repair_fields_enabled_by_default,
            repair_fields_by_source=config.export.repair_fields_by_source,
            direct_links_enabled_by_source=config.export.direct_links_enabled_by_source,
            include_terms=config.include_terms,
            exclude_terms=config.exclude_terms,
            excluded_title_patterns=config.excluded_title_patterns,
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


def _validate_artifact_paths(config: SearchConfig, replay_from: Path | None) -> None:
    paths = {
        "Database": config.database_path,
        "Log": config.log_file,
        **{path.name: path for path in export_paths(config.export_dir, config.export.formats)},
    }
    if replay_from is not None and replay_from.resolve() != config.database_path.resolve():
        paths["Replay archive"] = replay_from
    used: dict[Path, str] = {}
    for label, path in paths.items():
        resolved = path.resolve()
        if resolved in used:
            raise typer.BadParameter(f"{label} and {used[resolved]} point to the same file: {resolved}")
        used[resolved] = label


def _resolve_run_scope(
    config: SearchConfig, *, locations_overridden: bool, replay_from: Path | None, reroute: bool, preview: bool = False
) -> tuple[SearchConfig, str]:
    if reroute and config.search_locations:
        try:
            if locations_overridden and config.country.casefold() == "france":
                # Board-specific place lists refer to the towns in the saved profile.
                config.jobspy.google_query_places = ()
                config.jobspy.glassdoor_regional_fallback_term = None
                config.jobspy.glassdoor_location_override = None
                config.aggregators.location_terms = ()
                config.aggregators.direct_locations = ()
            routed = (
                route_location_overrides(config, write_cache=False, cache_only=True)
                if preview
                else route_location_overrides(config, write_cache=True)
            )
        except (httpx.HTTPError, ValueError) as exc:
            raise typer.BadParameter(
                f"Could not route the requested locations: {exc}", param_hint="--location"
            ) from exc
        if not preview or routed is not config:
            return routed, "resolved from --location" if locations_overridden else "resolved from locations"
    scope = load_location_scope(config, replay_from or config.database_path, warn=not preview)
    return config.model_copy(update={"location_scope": scope}), "saved geographic scope"


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
    semaphore = asyncio.Semaphore(max(1, config.custom_source_concurrency))
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
        return {source: 0}, {source: str(exc)}
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
    return SearchConfig(**overrides)


def main() -> None:
    if sys.argv[1:2] == ["sync"]:
        from scrapper.tracker_sync import main as sync_main

        sync_main(sys.argv[2:])
    else:
        app()


if __name__ == "__main__":
    main()
