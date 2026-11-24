from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
import yaml

from jobsfr.config.settings import EXAMPLE_CONFIG_FILE, PROJECT_ROOT, default_local_config, profile_paths


def example_config_hint() -> str:
    if PROJECT_ROOT is not None:
        return f"See the example YAML at\n  {EXAMPLE_CONFIG_FILE}"
    return "To view the example YAML, run\n  jobsfr setup --example"


def setup(
    profile: Annotated[Path | None, typer.Option(metavar="FILE", help="Save to a new YAML profile.")] = None,
    example: Annotated[bool, typer.Option("--example", help="Print the example YAML.")] = False,
) -> None:
    """Create a search profile by answering two questions."""
    if example:
        try:
            example_yaml = EXAMPLE_CONFIG_FILE.read_text(encoding="utf-8")
        except OSError as exc:
            raise typer.BadParameter(f"Could not read the example YAML: {exc}") from exc
        typer.echo(example_yaml, nl=False)
        return
    path = profile.expanduser().absolute() if profile is not None else default_local_config()
    if path.exists():
        raise typer.BadParameter(f"Configuration already exists at {path}. Edit it or choose a new --profile file.")

    typer.echo(example_config_hint())
    query = typer.prompt("Job title to search for").strip()
    location = typer.prompt("French town to search in (e.g. Toulouse)").strip()
    if not query or not location:
        raise typer.BadParameter("Enter both a job title and a town.")

    settings: dict[str, object] = {
        "queries": [query],
        "locations": [location],
        "filters": {"title": {"include": [query]}},
    }
    try:
        if profile is not None:
            settings["paths"] = profile_paths(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as file:
            yaml.safe_dump(settings, file, allow_unicode=True, sort_keys=False)
    except OSError as exc:
        raise typer.BadParameter(f"Could not save configuration: {exc}") from exc

    typer.echo(f"Saved search settings to {path}")
    profile_option = " --profile FILE" if profile is not None else ""
    if profile is not None:
        typer.echo("Use the saved profile path in place of FILE below.")
    typer.echo(f"Review your settings with jobsfr status{profile_option}.")
    typer.echo(f"For automatic runs with Homebrew or a Linux checkout, run jobsfr schedule enable{profile_option}.")
    typer.echo(f"To search now, run jobsfr search{profile_option}.")
