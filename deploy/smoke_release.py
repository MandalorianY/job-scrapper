"""Exercise an installed CLI or standalone release without scraping."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import load_workbook

from scrapper import __version__
from scrapper.models import JobOffer
from scrapper.storage import JobStore


def smoke(command: list[str], workspace: Path) -> None:
    example = "Example coordinator"
    profile = workspace / "profil été.yaml"
    profile.write_text(
        "queries: [example]\ninclude_terms: [example]\nlocation_scope: {enabled: false}\n"
        'export: {excel_columns: ["Intitulé", "À vérifier"]}\n'
        "paths:\n  database_path: custom data/jobs.duckdb\n  log_file: custom data/run.jsonl\n"
        "  export_dir: custom exports\n",
        encoding="utf-8",
    )
    (workspace / "config.local.yaml").write_text("paths: [invalid\n", encoding="utf-8")
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
    finally:
        store.close()

    environment = {**os.environ, "PYTHONUTF8": "1", "PYTHONTZPATH": "", "NO_COLOR": "1"}
    environment.pop("SCRAPPER_DISABLE_LOCAL_CONFIG", None)

    def run(*args: str) -> str:
        result = subprocess.run(
            [*command, *args],
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            stdin=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"CLI failed for {args!r}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()

    assert run("--version") == f"job-scrapper {__version__}"
    help_text = run("--help")
    assert "Usage:" in help_text, help_text
    assert "Search scope" in run("--profile", profile.name, "--status")
    settings = json.loads(run("--profile", profile.name, "--show-config"))["values"]
    assert settings["queries"] == ["example"]
    if not all(Path(path).resolve().is_relative_to(workspace.resolve()) for path in settings["paths"].values()):
        raise RuntimeError("Run the release smoke test against an installed wheel or standalone executable.")
    run("--profile", profile.name, "--replay-from", archive.name)
    run("--profile", profile.name, "--export-only")
    exports = workspace / "custom exports"
    workbook = load_workbook(exports / "jobs.xlsx")
    try:
        assert "Données brutes" in workbook.sheetnames
        sheet = workbook["Offres"]
        assert sheet.max_row == 2 and "TableOffres" in sheet.tables
        assert "Date limite dépassée" in sheet["B2"].value
    finally:
        workbook.close()
    assert all(example in (exports / filename).read_text(encoding="utf-8") for filename in ("jobs.csv", "jobs_raw.csv"))
    assert (workspace / "custom data/run.jsonl").is_file()
    print(f"Verified {command[0]} with a YAML profile, replay and offline exports.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("Pass the installed CLI command to check.")
    with TemporaryDirectory(prefix="job-scrapper-été-") as temporary:
        smoke(sys.argv[1:], Path(temporary))
