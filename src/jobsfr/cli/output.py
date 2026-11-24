"""Render the CLI status screens and export summary."""

from __future__ import annotations

from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from jobsfr.cli.profile_setup import example_config_hint
from jobsfr.config import SearchConfig
from jobsfr.config.settings import PACKAGE_CONFIG_DIR, default_local_config
from jobsfr.scrapers import JOBSPY_SITES
from jobsfr.scrapers.aggregators import AGGREGATORS_SOURCE
from jobsfr.scrapers.wttj import WTTJ_SOURCE

console = Console()


def print_search_setup_error(message: str) -> None:
    text = Text(f"Invalid value: {message}\n\n{example_config_hint()}")
    text.highlight_words(["jobsfr setup --example", "jobsfr setup"], style="bold cyan")
    Console(stderr=True).print(Panel(text, title="Error", title_align="left", border_style="red"))


def print_tracker_sync_status(config: SearchConfig) -> None:
    enabled = config.sync is not None and config.sync.enabled
    status = Text("✓ Enabled", style="bold green") if enabled else Text("✕ Disabled", style="bold yellow")
    console.print(Text.assemble(("Tracker sync  ", "bold"), status))
    console.print()


def print_search_status(config: SearchConfig, setting_sources: dict[str, str], profile: Path | None) -> None:
    local_config = default_local_config()
    scope = config.location_scope
    centers = ", ".join(center.name for center in scope.centers)
    location_filter = "Disabled"
    if scope.enabled:
        location_filter = f"{scope.radius_km:g} km around {centers}" if centers else "No resolved towns"
    direct_regions = ", ".join(config.aggregators.direct_locations)
    if not direct_regions and config.country.casefold() == "france" and config.search_locations:
        direct_regions = "Derived from search locations"
    rules = config.filters.model_dump(mode="json", exclude_defaults=True)
    filter_text = yaml.safe_dump(rules, allow_unicode=True, sort_keys=False).strip() if rules else ""
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
        profile_name = local_config.name
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
            (("Listing filters", filter_text),),
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
    console.print(
        Text(f"Use {local_config} for local settings. Use --profile FILE for a specific YAML profile."), width=width
    )
    console.print()
    override_lines = "\n".join(f"{name}: {', '.join(sorted(fields))}" for name, fields in overrides.items())
    console.print(Text(override_lines or "None"), width=width)
    console.print()


def print_source_status(config: SearchConfig) -> None:
    aggregators = config.aggregators
    groups = (
        ("JobSpy", JOBSPY_SITES, True),
        ("Custom", (WTTJ_SOURCE,), True),
        (
            "Aggregator",
            tuple(spec.source for spec in (*aggregators.direct_specs, *aggregators.site_search_sources)),
            config.is_source_enabled(AGGREGATORS_SOURCE),
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
