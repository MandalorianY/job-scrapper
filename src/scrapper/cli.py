from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from scrapper.config import SearchConfig
from scrapper.export import export_jobs
from scrapper.location_scope import location_is_in_scope
from scrapper.scrapers import (
    scrape_hellowork,
    scrape_aggregators,
    scrape_jobspy_sources,
    scrape_web_search,
    scrape_welcome_to_the_jungle,
)
from scrapper.storage import JobStore


app = typer.Typer(help="Scrape example role jobs and export deduped CSV/XLSX files.")
console = Console()


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
) -> None:
    config = _config_from_cli(
        query,
        locations,
        db,
        export_dir,
        results_wanted,
        jobspy_hours_old,
        custom_pages,
        refresh_known,
    )
    store = JobStore(
        config.database_path,
        source_priority=config.dedupe_source_priority,
        default_source_priority=config.dedupe_default_source_priority,
        dedupe_primary_fields=config.dedupe_fields.primary,
        dedupe_fallback_fields=config.dedupe_fields.fallback,
        dedupe_description_fields=config.dedupe_fields.description,
    )
    try:
        counts: dict[str, int] = {}
        if not skip_jobspy:
            console.print("[bold]Running JobSpy sources[/bold] (Indeed, LinkedIn, Glassdoor, Google Jobs)")
            counts.update(scrape_jobspy_sources(config, store))
        if not skip_custom:
            console.print("[bold]Running custom/free sources[/bold] (HelloWork, Welcome to the Jungle, web search, aggregators)")
            if config.is_source_enabled(config.hellowork.source):
                counts["hellowork"] = asyncio.run(scrape_hellowork(config, store))
            if config.is_source_enabled(config.welcome_to_the_jungle.source):
                counts["welcome_to_the_jungle"] = asyncio.run(scrape_welcome_to_the_jungle(config, store))
            if config.is_source_enabled(config.web_search.source):
                counts["web_search"] = asyncio.run(scrape_web_search(config, store))
            if config.is_source_enabled(config.aggregators.source):
                counts.update(asyncio.run(scrape_aggregators(config, store)))

        purged = store.purge_excluded_offers(config.exclude_terms)
        if purged:
            counts["excluded_removed"] = purged
        purged_nonmatching = store.purge_nonmatching_offers(config.include_terms)
        if purged_nonmatching:
            counts["nonmatching_removed"] = purged_nonmatching
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
            include_terms=config.include_terms,
            exclude_terms=config.exclude_terms,
            location_allowed=lambda location: location_is_in_scope(location, config.location_scope),
        )
        _print_summary(counts, total, excel_path, csv_path, raw_csv_path)
    finally:
        store.close()


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


def _config_from_cli(
    query: list[str] | None,
    locations: list[str] | None,
    db: Path | None,
    export_dir: Path | None,
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
    if db is not None or export_dir is not None:
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
