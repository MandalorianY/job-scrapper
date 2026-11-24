"""Exercise an installed CLI or standalone release without scraping."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import load_workbook
from rich.text import Text

from jobsfr import CLI_NAME, __version__
from jobsfr.config.settings import EXAMPLE_CONFIG_FILE
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


def smoke(command: list[str], workspace: Path) -> None:
    example = "Example coordinator"
    profile = workspace / "profil été.yaml"
    profile.write_text(
        "queries: [example]\n"
        'locations: ["Toulouse, France"]\n'
        "filters:\n  title: {include: [example]}\n"
        "  company: {exclude: {types: [esn, recruitment_agency, temporary_staffing]}}\n"
        "location_scope: {enabled: false}\n"
        'export: {excel_columns: ["Intitulé", "À vérifier"]}\n'
        "paths:\n  database_path: custom data/jobs.duckdb\n  log_file: custom data/run.jsonl\n"
        "  export_dir: custom exports\n",
        encoding="utf-8",
    )
    archive = workspace / "observation archive.duckdb"
    store = JobStore(archive)
    try:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.test/jobs/1",
                title=example,
                company="Example employer",
                location="Toulouse, France",
                employment_type="CDI",
                description="Manage a full-time example program for a local team.",
                raw={"json_ld": {"validThrough": "2000-01-01T00:00:00+01:00"}},
            )
        )
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.test/jobs/2",
                title=example,
                company="Capgemini",
                employment_type="CDI",
            )
        )
    finally:
        store.close()

    home = workspace / "home"
    app_dir = "jobsfr"
    environment = {
        **{key: value for key, value in os.environ.items() if not key.upper().startswith("JOBSFR_")},
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData/Roaming"),
        "LOCALAPPDATA": str(home / "AppData/Local"),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"),
        "XDG_STATE_HOME": str(home / "state"),
        "PYTHONUTF8": "1",
        "PYTHONTZPATH": "",
        "NO_COLOR": "1",
    }

    def run(*args: str, input_text: str | None = None, expected_code: int = 0) -> str:
        result = subprocess.run(
            [*command, *args],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            stdin=subprocess.DEVNULL if input_text is None else None,
            input=input_text,
            check=False,
        )
        if result.returncode != expected_code:
            raise RuntimeError(f"CLI failed for {args!r}\n{result.stdout}\n{result.stderr}")
        output = result.stderr if expected_code else result.stdout
        return Text.from_ansi(output).plain.strip()

    assert run("--version") == f"{CLI_NAME} {__version__}"
    help_text = run("--help")
    assert "Usage:" in help_text, help_text
    assert run() == help_text
    for name in ("setup", "search", "export", "replay", "status", "schedule", "sync"):
        assert name in help_text and "usage:" in run(name, "--help").lower(), name
    assert run("setup", "--example") == EXAMPLE_CONFIG_FILE.read_text(encoding="utf-8").strip()
    assert "jobsfr setup" in " ".join(run("search", expected_code=2).split())
    guidance = run("setup", input_text=f"{example}\nToulouse\n")
    assert "jobsfr schedule enable" in guidance and "jobsfr search" in guidance
    assert "--yes" in run("schedule", "enable", "--help")
    assert "schedule disable" in run("schedule", "disable", "--help")
    configured = json.loads(run("status", "--json"))["values"]
    assert configured["queries"] == [example]
    assert configured["locations"] == ["Toulouse"]
    defaults = configured["paths"]
    if sys.platform == "darwin":
        config_dir = home / ".config" / app_dir
        data_dir = home / "Library/Application Support" / app_dir
        log_dir = home / "Library/Logs" / app_dir
    elif sys.platform == "win32":
        config_dir = Path(environment["APPDATA"]) / app_dir
        data_dir = Path(environment["LOCALAPPDATA"]) / app_dir
        log_dir = data_dir / "Logs"
    else:
        config_dir = Path(environment["XDG_CONFIG_HOME"]) / app_dir
        data_dir = Path(environment["XDG_DATA_HOME"]) / app_dir
        log_dir = Path(environment["XDG_STATE_HOME"]) / app_dir
    assert Path(defaults["database_path"]) == data_dir / "job_search.duckdb"
    assert Path(defaults["log_file"]) == log_dir / "jobsfr.jsonl"
    export_dir = Path(defaults["export_dir"])
    output_args: list[str] = []
    if sys.platform == "win32":
        assert export_dir.is_absolute() and export_dir.name == app_dir
        # The Windows shell resolves the real account's Documents folder.
        export_dir = workspace / "windows exports"
        output_args = ["--export-dir", str(export_dir)]
    else:
        assert export_dir == home / "Documents" / app_dir

    run("replay", "--archive", archive.name, *output_args)
    assert (data_dir / "job_search.duckdb").is_file()
    assert (log_dir / "jobsfr.jsonl").is_file()
    assert (export_dir / "jobs.xlsx").is_file()

    config_dir.mkdir(parents=True, exist_ok=True)
    local_config = config_dir / "config.yaml"
    local_config.write_text("queries: [installed]\n", encoding="utf-8")
    installed = json.loads(run("status", "--json"))
    assert installed["values"]["queries"] == ["installed"]
    assert Path(installed["sources"]["queries"]) == local_config
    local_config.write_text("paths: [invalid\n", encoding="utf-8")
    assert "Search scope" in run("status", "--profile", profile.name)
    settings = json.loads(run("status", "--json", "--profile", profile.name))["values"]
    assert settings["queries"] == ["example"]
    if not all(Path(path).resolve().is_relative_to(workspace.resolve()) for path in settings["paths"].values()):
        raise RuntimeError("Run the release smoke test against an installed wheel or standalone executable.")
    run("replay", "--archive", archive.name, "--profile", profile.name)
    run("export", "--profile", profile.name)
    exports = workspace / "custom exports"
    workbook = load_workbook(exports / "jobs.xlsx")
    try:
        assert workbook["Données brutes"].max_row == 3
        sheet = workbook["Offres"]
        assert sheet.max_row == 2 and "TableOffres" in sheet.tables
        assert "Date limite dépassée" in sheet["B2"].value
    finally:
        workbook.close()
    assert all(example in (exports / filename).read_text(encoding="utf-8") for filename in ("jobs.csv", "jobs_raw.csv"))
    assert (workspace / "custom data/run.jsonl").is_file()
    assert run("sync", "--profile", profile.name, "--status") == "Tracker sync disabled."
    print(f"Verified {command[0]} with installed defaults, a YAML profile, replay, exports and tracker sync.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Pass the installed CLI command to check.")
    with TemporaryDirectory(prefix=f"{CLI_NAME}-été-") as temporary:
        smoke(sys.argv[1:], Path(temporary))
