"""Render the CLI status screens and export summary."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.text import Text

from scrapper.config import SearchConfig
from scrapper.config.settings import LOCAL_CONFIG_FILE, PACKAGE_CONFIG_DIR
from scrapper.scrapers import JOBSPY_SITES
from scrapper.scrapers.aggregators import AGGREGATORS_SOURCE
from scrapper.scrapers.wttj import WTTJ_SOURCE

console = Console()
CONFIG_OVERRIDE_HELP = (
    "Use config.local.yaml in the working directory for an installed CLI, or under src/scrapper/config "
    "in a checkout. Use --profile FILE for a specific YAML profile."
)


def print_tracker_sync_status(config: SearchConfig) -> None:
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


def print_search_status(config: SearchConfig, setting_sources: dict[str, str], profile: Path | None) -> None:
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
    console.print(CONFIG_OVERRIDE_HELP, width=width)
    console.print()
    override_lines = "\n".join(f"{name}: {', '.join(sorted(fields))}" for name, fields in overrides.items())
    console.print(Text(override_lines or "None"), width=width)
    console.print()


def print_source_status(config: SearchConfig, *, skip_jobspy: bool, skip_custom: bool) -> None:
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


def print_export_summary(counts: dict[str, int], total: int, outputs: tuple[Path, ...]) -> None:
    table = Table(title="Job search export", title_style="bold cyan", header_style="bold cyan")
    table.add_column("Source")
    table.add_column("Stored / updated", justify="right")
    for source, count in counts.items():
        style = "yellow" if source.endswith("_removed") else "green" if count else "dim"
        table.add_row(Text(source), Text(str(count), style=style))
    table.add_section()
    table.add_row("Deduped total", str(total), style="bold green" if total else "bold yellow")
    console.print(table)
    for path in outputs:
        console.print(Text.assemble((f"{path.name}: ", "bold cyan"), (str(path), "green")))
