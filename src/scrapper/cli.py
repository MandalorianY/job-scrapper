from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated

import httpx
import structlog
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from scrapper.config import SearchConfig
from scrapper.config.settings import LOCAL_CONFIG_FILE, PACKAGE_CONFIG_DIR, _leaf_values
from scrapper.export import export_jobs
from scrapper.location_scope import location_is_in_scope
from scrapper.logging import DEFAULT_LOG_LEVEL, LogLevel, configure_logging
from scrapper.scrapers import (
    JOBSPY_SITES,
    scrape_aggregators,
    scrape_jobspy_sources,
    scrape_welcome_to_the_jungle,
)
from scrapper.scrapers.aggregators import AGGREGATORS_SOURCE
from scrapper.scrapers.async_http import SourceBlockedError
from scrapper.scrapers.wttj import WTTJ_SOURCE
from scrapper.search_regions import load_location_scope, route_location_overrides, save_location_scope
from scrapper.storage import JobStore

app = typer.Typer()
console = Console()
log = structlog.get_logger(__name__)
ProgressReporter = Callable[[str], None]
_CONFIG_OVERRIDE_HELP = (
    "Create or edit src/scrapper/config/config.local.yaml to apply overrides automatically. "
    "Use --profile PATH to load a different YAML file."
)


@app.command(
    help="[bold cyan]Scrape example role jobs and export deduped CSV/XLSX files.[/bold cyan]",
    epilog=_CONFIG_OVERRIDE_HELP,
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
    export_dir: Annotated[Path | None, typer.Option("--export-dir", help="Folder for CSV and Excel exports.")] = None,
    results_wanted: Annotated[int | None, typer.Option("--results-wanted", min=1)] = None,
    custom_pages: Annotated[int | None, typer.Option("--custom-pages", min=1)] = None,
    export_only: Annotated[
        bool, typer.Option("--export-only", help="Regenerate local exports from DuckDB without scraping.")
    ] = False,
    skip_custom: Annotated[bool, typer.Option("--skip-custom", help="Only run JobSpy sources.")] = False,
    skip_jobspy: Annotated[bool, typer.Option("--skip-jobspy", help="Only run custom sources.")] = False,
    replay_from: Annotated[
        Path | None,
        typer.Option(exists=True, dir_okay=False, help="Rebuild from a DuckDB observation archive offline."),
    ] = None,
    log_file: Annotated[Path | None, typer.Option("--log-file", help="Structured JSONL log path.")] = None,
    profile: Annotated[
        Path | None,
        typer.Option("--profile", exists=True, dir_okay=False, help="Use this YAML in place of config.local.yaml."),
    ] = None,
    log_level: Annotated[
        LogLevel, typer.Option("--log-level", help="Minimum structured log level.")
    ] = DEFAULT_LOG_LEVEL,
    show_config: Annotated[
        bool, typer.Option("--show-config", help="Print effective settings and their sources, then exit.")
    ] = False,
    status: Annotated[
        bool, typer.Option("--status", help="Show search settings, job sources, and tracker sync status offline.")
    ] = False,
) -> None:
    if export_only:
        skip_custom = True
        skip_jobspy = True
    config_sources = {} if show_config or status else None
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
    status_only = status and not show_config
    if status_only:
        _print_tracker_sync_status(config)
    live_search = replay_from is None and not (skip_jobspy and skip_custom)
    unrouted_config = config
    if live_search and not config.search_locations and config_sources is None:
        raise typer.BadParameter("Set locations in your search config or pass --location.", param_hint="--location")
    before_routing = dict(_leaf_values(config.model_dump())) if config_sources is not None else {}
    if live_search and config.search_locations and not status_only:
        try:
            if locations is not None and config.country.casefold() == "france":
                # Board-specific place lists in a saved profile refer to its original towns.
                config.jobspy.google_query_places = ()
                config.jobspy.glassdoor_regional_fallback_term = None
                config.jobspy.glassdoor_location_override = None
                config.aggregators.location_terms = ()
                config.aggregators.direct_locations = ()
            config = route_location_overrides(config, write_cache=not show_config)
        except (httpx.HTTPError, ValueError) as exc:
            raise typer.BadParameter(
                f"Could not route the requested locations: {exc}", param_hint="--location"
            ) from exc
        scope_origin = "resolved from --location" if locations else "resolved from locations"
    else:
        scope = load_location_scope(config, replay_from or config.database_path)
        config = config.model_copy(update={"location_scope": scope})
        scope_origin = "saved geographic scope"
    if config_sources is not None:
        for key, value in _leaf_values(config.model_dump()):
            if key not in before_routing or before_routing[key] != value:
                config_sources[".".join(key)] = scope_origin
        report = config.describe(config_sources)
        if show_config:
            typer.echo(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            _print_search_status(config, report["sources"], profile)
            _print_source_status(
                config,
                skip_jobspy=skip_jobspy or replay_from is not None,
                skip_custom=skip_custom or replay_from is not None,
            )
        return
    configured_log_file = configure_logging(config.log_file, level=log_level)
    if live_search:
        save_location_scope(unrouted_config, config.location_scope)
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
    store = _job_store(config, run_id)
    location_allowed = (
        (lambda location: location_is_in_scope(location, config.location_scope))
        if config.location_scope.centers
        else None
    )
    try:
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
        if locations is None and location_allowed is not None:
            purged_out_of_scope = store.purge_out_of_scope_offers(location_allowed)
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
            location_allowed=location_allowed,
        )
        _print_summary(counts, total, excel_path, csv_path, raw_csv_path)
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
            excel_path=str(excel_path),
            csv_path=str(csv_path),
            raw_csv_path=str(raw_csv_path),
            duration_seconds=round(time.monotonic() - started_at, 3),
        )
    except Exception:
        log.exception("scrape_run_failed", duration_seconds=round(time.monotonic() - started_at, 3))
        raise
    finally:
        structlog.contextvars.clear_contextvars()
        store.close()


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


def _print_tracker_sync_status(config: SearchConfig) -> None:
    # The tracker sync entry point also imports this CLI to run the scraper.
    from scrapper.tracker_sync import load_sync_settings, sync_settings_path

    try:
        settings = load_sync_settings(sync_settings_path(config.database_path))
    except (OSError, ValueError):
        status = Text("⚠ Unavailable (could not read settings)", style="bold yellow")
    else:
        status = Text("✓ Enabled", style="bold green") if settings.enabled else Text("✕ Disabled", style="bold yellow")
    console.print(Text.assemble(("Tracker sync  ", "bold"), status))
    console.print()


def _print_search_status(config: SearchConfig, setting_sources: dict[str, str], profile: Path | None) -> None:
    scope = config.location_scope
    centers = ", ".join(center.name for center in scope.centers)
    location_filter = "Disabled"
    if scope.enabled:
        location_filter = f"{scope.radius_km:g} km around {centers}" if centers else "No resolved towns"
    direct_regions = ", ".join(config.aggregators.direct_locations)
    if not direct_regions and config.country.casefold() == "france" and config.search_locations:
        direct_regions = "Derived from search locations"
    company_exclusions = "\n".join(
        f"{source}: {', '.join(companies)}"
        for source, companies in config.excluded_companies_by_source.items()
        if companies
    )
    search_limits = (
        f"{config.results_wanted_per_query} results per query, {config.custom_max_pages} custom list pages, "
        f"{config.aggregators.max_detail_pages} aggregator detail pages"
    )
    overrides: dict[str, set[str]] = {}
    base_config = str(PACKAGE_CONFIG_DIR / "config.yaml")
    for setting, source in setting_sources.items():
        if source not in {"default", base_config}:
            overrides.setdefault(Path(source).name, set()).add(setting.split(".", 1)[0])
    if profile is None:
        profile_name = LOCAL_CONFIG_FILE.name
        profile_applied = profile_name in overrides
    else:
        profile_name = profile.name
        profile_applied = str(profile.expanduser().absolute()) in setting_sources.values()

    sections = (
        (
            "Search scope",
            (
                ("Environment", config.app_env),
                ("Search profile", f"{profile_name} (overrides applied)" if profile_applied else "None"),
                ("Search locations", ", ".join(config.search_locations)),
                ("Search radius", f"{config.distance_km} km"),
                ("Location filter", location_filter),
                ("Direct search regions", direct_regions),
                ("Queries", ", ".join(config.queries)),
                ("Search limits", search_limits),
            ),
        ),
        (
            "Filters",
            (
                ("Include terms", ", ".join(config.include_terms)),
                ("Exclude terms", ", ".join(config.exclude_terms)),
                ("Excluded companies", company_exclusions),
                ("Excluded titles", "\n".join(config.excluded_title_patterns)),
            ),
        ),
    )
    width = min(console.width, 96)
    for heading, rows in sections:
        console.print(heading, style="bold cyan")
        table = Table.grid(padding=(0, 2))
        table.add_column(min_width=21)
        table.add_column()
        for label, value in rows:
            table.add_row(label, Text(value or "None"))
        console.print(table, width=width)
        console.print()

    console.print("Configuration overrides", style="bold cyan")
    console.print(_CONFIG_OVERRIDE_HELP, width=width)
    console.print()
    override_lines = "\n".join(f"{name}: {', '.join(sorted(fields))}" for name, fields in overrides.items())
    console.print(Text(override_lines or "None"), width=width)
    console.print()


def _print_source_status(config: SearchConfig, *, skip_jobspy: bool, skip_custom: bool) -> None:
    aggregators = config.aggregators
    groups = (
        ("JobSpy", JOBSPY_SITES, not skip_jobspy),
        ("Custom", (WTTJ_SOURCE,), not skip_custom),
        (
            "Aggregator",
            tuple(spec.source for spec in (*aggregators.direct_specs, *aggregators.site_search_sources)),
            not skip_custom and config.is_source_enabled(AGGREGATORS_SOURCE),
        ),
    )
    table = Table("Source", "Group", "Status", title="Job sources", title_style="bold cyan", header_style="bold cyan")
    for group, sources, group_enabled in groups:
        for source in sources:
            enabled = group_enabled and config.is_source_enabled(source)
            status = Text("Enabled", style="bold green") if enabled else Text("Disabled", style="bold yellow")
            table.add_row(Text(source), group, status)
    console.print(table)


def _print_summary(counts: dict[str, int], total: int, excel_path: Path, csv_path: Path, raw_csv_path: Path) -> None:
    table = Table(title="Job search export", title_style="bold cyan", header_style="bold cyan")
    table.add_column("Source")
    table.add_column("Stored / updated", justify="right")
    for source, count in counts.items():
        style = "yellow" if source.endswith("_removed") else "green" if count else "dim"
        table.add_row(Text(source), Text(str(count), style=style))
    table.add_section()
    table.add_row("Deduped total", str(total), style="bold green" if total else "bold yellow")
    console.print(table)
    for label, path in (("Excel: ", excel_path), ("CSV:   ", csv_path), ("Raw:   ", raw_csv_path)):
        console.print(Text.assemble((label, "bold cyan"), (str(path), "green")))


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
        name: path.absolute()
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


if __name__ == "__main__":
    app()
