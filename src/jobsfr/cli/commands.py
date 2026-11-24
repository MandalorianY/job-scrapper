"""Command-line help, options, and dispatch."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from jobsfr import CLI_NAME, __version__
from jobsfr.cli.options import ProfileOption, existing_file
from jobsfr.cli.profile_setup import setup
from jobsfr.cli.runner import run
from jobsfr.cli.scheduling import schedule_app
from jobsfr.cli.tracker_sync import sync
from jobsfr.runtime.logging import DEFAULT_LOG_LEVEL, LogLevel

app = typer.Typer(pretty_exceptions_show_locals=False)


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"{CLI_NAME} {__version__}")
        raise typer.Exit()


QueryOption = Annotated[list[str] | None, typer.Option("--query", "-q", help="Search query. Repeat to set multiple.")]
LocationOption = Annotated[
    list[str] | None, typer.Option("--location", "-l", help="Search town. Repeat to set multiple.")
]
DatabaseOption = Annotated[Path | None, typer.Option("--db", help="DuckDB storage path.")]
ExportDirOption = Annotated[Path | None, typer.Option("--export-dir", help="Folder for the export files.")]
LogFileOption = Annotated[Path | None, typer.Option("--log-file", help="Structured JSONL log path.")]
LogLevelOption = Annotated[LogLevel, typer.Option("--log-level", help="Minimum structured log level.")]
ResultsOption = Annotated[int | None, typer.Option("--results-wanted", min=1, help="Maximum results per query.")]
PagesOption = Annotated[int | None, typer.Option("--custom-pages", min=1, help="Maximum list pages per custom source.")]
SkipCustomOption = Annotated[bool, typer.Option("--skip-custom", help="Skip custom sources.")]
SkipJobspyOption = Annotated[bool, typer.Option("--skip-jobspy", help="Skip JobSpy sources.")]


@app.callback(
    invoke_without_command=True,
    no_args_is_help=False,
    help="Search job boards in France and manage your application tracker.",
    epilog="Start with jobsfr setup. Run jobsfr COMMAND --help for command options.",
)
def _root(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_show_version, is_eager=True, help="Show the installed version.")
    ] = False,
) -> None:
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())


@app.command(rich_help_panel="Job listings")
def search(
    profile: ProfileOption = None,
    query: QueryOption = None,
    locations: LocationOption = None,
    results_wanted: ResultsOption = None,
    custom_pages: PagesOption = None,
    skip_custom: SkipCustomOption = False,
    skip_jobspy: SkipJobspyOption = False,
    db: DatabaseOption = None,
    export_dir: ExportDirOption = None,
    log_file: LogFileOption = None,
    log_level: LogLevelOption = DEFAULT_LOG_LEVEL,
) -> None:
    """Search job boards and update the tracker."""
    if skip_custom and skip_jobspy:
        raise typer.BadParameter("Choose at least one source group, or run jobsfr export to use saved listings.")
    run(
        profile=profile,
        query=query,
        locations=locations,
        results_wanted=results_wanted,
        custom_pages=custom_pages,
        skip_custom=skip_custom,
        skip_jobspy=skip_jobspy,
        db=db,
        export_dir=export_dir,
        log_file=log_file,
        log_level=log_level,
    )


@app.command(rich_help_panel="Job listings")
def export(
    profile: ProfileOption = None,
    db: DatabaseOption = None,
    export_dir: ExportDirOption = None,
    log_file: LogFileOption = None,
    log_level: LogLevelOption = DEFAULT_LOG_LEVEL,
) -> None:
    """Regenerate the tracker and exports from saved listings."""
    run(profile=profile, db=db, export_dir=export_dir, log_file=log_file, log_level=log_level, export_only=True)


@app.command(rich_help_panel="Job listings")
def replay(
    archive: Annotated[Path, typer.Option(parser=existing_file, metavar="FILE", help="DuckDB observation archive.")],
    profile: ProfileOption = None,
    db: DatabaseOption = None,
    export_dir: ExportDirOption = None,
    log_file: LogFileOption = None,
    log_level: LogLevelOption = DEFAULT_LOG_LEVEL,
) -> None:
    """Rebuild listings and exports from an observation archive."""
    run(profile=profile, db=db, export_dir=export_dir, log_file=log_file, log_level=log_level, replay_from=archive)


@app.command(rich_help_panel="Setup and maintenance")
def status(
    profile: ProfileOption = None,
    db: DatabaseOption = None,
    json_output: Annotated[
        bool, typer.Option("--json", help="Print effective settings and their origins as JSON.")
    ] = False,
) -> None:
    """Inspect settings, sources, and tracker sync offline."""
    run(profile=profile, db=db, report="json" if json_output else "text")


app.command(rich_help_panel="Setup and maintenance")(setup)
app.add_typer(schedule_app, name="schedule", rich_help_panel="Setup and maintenance")
app.command(rich_help_panel="Setup and maintenance")(sync)


def main() -> None:
    app()
