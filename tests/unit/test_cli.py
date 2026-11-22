import asyncio
import json
from pathlib import Path
from typing import Never

import pytest
from openpyxl import load_workbook
from typer.testing import CliRunner

from scrapper import cli, search_regions
from scrapper.config import SearchConfig
from scrapper.config.settings import LocationScopeConfig
from scrapper.models import JobOffer
from scrapper.scrapers.async_http import SourceBlockedError
from scrapper.storage import JobStore


def test_export_only_preserves_existing_tracker_if_database_is_missing(tmp_path: Path) -> None:
    workbook = tmp_path / "jobs.xlsx"
    workbook.write_bytes(b"existing tracker")
    database = tmp_path / "missing.duckdb"
    result = CliRunner().invoke(cli.app, ["--export-only", "--db", str(database), "--export-dir", str(tmp_path)])
    assert result.exit_code == 2
    assert "Database file not found" in result.output
    assert workbook.read_bytes() == b"existing tracker"
    assert not database.exists()


def test_export_only_applies_title_and_description_filters_to_saved_listings_and_tracker(tmp_path: Path) -> None:
    database = tmp_path / "jobs.duckdb"
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "filters:\n"
        "  title: {include: [data analyst], exclude: [chef-de-projet]}\n"
        "  description:\n"
        "    include: [déplacements, analyses, astreintes]\n"
        "    exclude: [déplacements-fréquents, {regex: '\\bastreintes?\\b'}]\n"
        "  contract: {include: [CDI], exclude: [CDD]}\n"
        "location_scope: {enabled: false}\n"
        "export: {excel_columns: [Statut, Lien, Intitulé]}\n",
        encoding="utf-8",
    )
    store = JobStore(database)
    try:
        for slug, title, description in (
            ("blocked", "Data Analyst", "DÉPLACEMENTS FRÉQUENTS."),
            ("blocked-title", "Data Analyst — Chef de projet", "Analyses et tableaux de bord."),
            ("blocked-regex", "Data Analyst", "Astreintes le week-end."),
            ("kept", "Data Analyst", "Déplacements occasionnels."),
        ):
            store.upsert_offer(
                JobOffer(
                    source="example",
                    source_url=f"https://example.com/jobs/{slug}",
                    title=title,
                    company="Example",
                    employment_type="CDI",
                    description=description,
                )
            )
    finally:
        store.close()
    export_dir = tmp_path / "exports"
    export_dir.mkdir()
    (export_dir / "jobs.csv").write_text(
        "Lien,Intitulé,Description,Statut\n"
        "https://example.com/jobs/blocked,Data Analyst,,En Attente\n"
        "https://example.com/jobs/kept,Data Analyst,,Postulé\n"
        "https://example.com/jobs/historical,Data Analyst,Astreinte,En Attente\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "--export-only",
            "--profile",
            str(profile),
            "--db",
            str(database),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    book = load_workbook(export_dir / "jobs.xlsx")
    try:
        offers = book["Offres"]
        assert offers.max_row == 2
        assert offers["A2"].value == "Postulé"
        assert "https://example.com/jobs/kept" in str(offers["B2"].value)
        assert book["Données brutes"].max_row == 5
    finally:
        book.close()


def test_replay_rejects_log_path_pointing_to_archive(tmp_path: Path) -> None:
    archive = tmp_path / "archive.duckdb"
    archive.write_bytes(b"archive")
    database = tmp_path / "new.duckdb"
    result = CliRunner().invoke(
        cli.app, ["--replay-from", str(archive), "--log-file", str(archive), "--db", str(database)]
    )
    assert result.exit_code == 2
    assert "Replay archive and Log point to the same file" in result.output
    assert archive.read_bytes() == b"archive"
    assert not database.exists()


def test_company_types_filter_saved_offers_and_tracker_history(tmp_path: Path) -> None:
    database = tmp_path / "jobs.duckdb"
    profile = tmp_path / "profile.yaml"
    base_profile = "location_scope: {enabled: false}\nexport: {excel_columns: [Lien, Intitulé, Entreprise]}\n"
    title_filter = "filters:\n  title: {include: [designer]}\n"
    company_filter = "  company: {exclude: {types: [esn, recruitment_agency, temporary_staffing]}}\n"
    profile.write_text(base_profile + title_filter + company_filter, encoding="utf-8")
    store = JobStore(database)
    try:
        for slug, company in (("blocked", "Néosoft S.A.S."), ("kept", "Néosoft Studio")):
            store.upsert_offer(
                JobOffer(
                    source="example",
                    source_url=f"https://example.com/jobs/{slug}",
                    title="Designer",
                    company=company,
                    employment_type="CDI",
                )
            )
    finally:
        store.close()
    (tmp_path / "jobs.csv").write_text(
        "Lien,Intitulé,Entreprise\n"
        "https://example.com/jobs/staffing,Designer,Adecco\n"
        "https://example.com/jobs/recruiter,Designer,Michael Page\n",
        encoding="utf-8",
    )
    args = [
        "--export-only",
        "--profile",
        str(profile),
        "--db",
        str(database),
        "--export-dir",
        str(tmp_path),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    workbook = load_workbook(tmp_path / "jobs.xlsx")
    try:
        assert workbook["Offres"].max_row == 2
        assert workbook["Offres"]["C2"].value == "Néosoft Studio"
        assert workbook["Données brutes"].max_row == 3
    finally:
        workbook.close()

    profile.write_text(base_profile + title_filter, encoding="utf-8")
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "Néosoft S.A.S." in (tmp_path / "jobs.csv").read_text(encoding="utf-8")


def test_database_startup_failure_is_recorded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "jobs.duckdb"
    database.touch()
    log_path = tmp_path / "run.jsonl"

    def fail(config: SearchConfig, run_id: str) -> Never:
        raise OSError("Database is unavailable")

    monkeypatch.setattr(cli, "_job_store", fail)
    result = CliRunner().invoke(cli.app, ["--export-only", "--db", str(database), "--log-file", str(log_path)])
    assert result.exit_code == 1
    assert "Run failed: Database is unavailable" in result.output
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [record["event"] for record in records] == ["scrape_run_started", "scrape_run_failed"]
    assert records[0]["run_id"] == records[1]["run_id"]


def test_offline_export_reuses_geographic_scope_from_live_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    monkeypatch.setenv("SCRAPPER_LOCATION_SCOPE__INCLUDE_UNRECOGNIZED", "false")

    def resolve(config: SearchConfig, *, write_cache: bool) -> SearchConfig:
        assert not config.location_scope.centers
        assert write_cache
        return config.model_copy(update={"location_scope": toulouse_scope})

    def no_offers(config: SearchConfig, store: JobStore, *, progress: cli.ProgressReporter) -> dict[str, int]:
        return {}

    monkeypatch.setattr(cli, "route_location_overrides", resolve)
    monkeypatch.setattr(cli, "scrape_jobspy_sources", no_offers)
    args = [
        "--db",
        str(tmp_path / "jobs.duckdb"),
        "--export-dir",
        str(tmp_path),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    runner = CliRunner()
    live = runner.invoke(cli.app, [*args, "--skip-custom"])
    assert live.exit_code == 0, live.output

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Offline commands must reuse the saved coordinates")

    monkeypatch.setattr(cli, "route_location_overrides", forbidden)
    status = runner.invoke(cli.app, [*args, "--status"])
    assert status.exit_code == 0, status.output
    assert "25 km around Toulouse" in status.stdout
    assert "saved geographic scope" in status.stdout

    offline = runner.invoke(cli.app, [*args, "--export-only", "--show-config"])
    assert offline.exit_code == 0, offline.output
    report = json.loads(offline.stdout)
    assert report["values"]["location_scope"] == toulouse_scope.model_dump(mode="json")
    assert report["sources"]["location_scope.centers"] == "saved geographic scope"

    monkeypatch.setenv("SCRAPPER_DISTANCE_KM", "15")
    changed = runner.invoke(cli.app, [*args, "--export-only", "--show-config"])
    assert changed.exit_code == 0, changed.output
    assert json.loads(changed.stdout)["values"]["location_scope"]["centers"] == []


def test_disabling_aggregators_controls_dispatch_and_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def welcome_to_the_jungle(config: SearchConfig, store: JobStore) -> int:
        return 3

    async def unexpected(*args: object) -> Never:
        raise AssertionError("The disabled aggregator scraper ran")

    monkeypatch.setattr(cli, "scrape_welcome_to_the_jungle", welcome_to_the_jungle)
    monkeypatch.setattr(cli, "scrape_aggregators", unexpected)
    monkeypatch.setenv("SCRAPPER_ENABLED_SOURCES__AGGREGATORS", "false")
    config = SearchConfig(paths={"database_path": tmp_path / "jobs.duckdb"})
    store = JobStore(config.database_path)
    try:
        assert asyncio.run(cli._run_custom_sources(config, store)) == ({"welcome_to_the_jungle": 3}, {})
    finally:
        store.close()

    status = CliRunner().invoke(cli.app, ["--status"])
    assert status.exit_code == 0, status.output
    sources = {
        row[0]: row[-1]
        for line in status.stdout.splitlines()
        if line.startswith("│")
        for row in [tuple(cell.strip() for cell in line.strip("│").split("│"))]
    }
    assert sources["welcome_to_the_jungle"] == "Enabled"
    assert sources["cadremploi"] == "Disabled"


def test_local_export_survives_source_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    async def sources(config: SearchConfig, store: JobStore) -> tuple[dict[str, int], dict[str, str]]:
        store.start_search("blocked_board", "example coordinator", "Toulouse")
        store.finish_search("blocked_board", "example coordinator", "Toulouse", 0, error=SourceBlockedError.message)
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse",
            )
        )
        return {"example": 1}, {"unavailable_board": "Temporary source outage"}

    monkeypatch.setattr(cli, "_run_custom_sources", sources)
    monkeypatch.setattr(
        cli,
        "route_location_overrides",
        lambda config, *, write_cache: config.model_copy(update={"location_scope": toulouse_scope}),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "--skip-jobspy",
            "--location",
            "Toulouse, France",
            "--db",
            str(tmp_path / "jobs.duckdb"),
            "--export-dir",
            str(tmp_path / "exports"),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "unavailable_board failed" in result.output
    assert result.output.count("blocked_board blocked by a verification challenge.") == 1
    assert (tmp_path / "exports/jobs.xlsx").exists()


def test_export_only_regenerates_local_files_from_duckdb(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "jobs.duckdb"
    export_dir = tmp_path / "exports"
    store = JobStore(database)
    try:
        store.upsert_offer(
            JobOffer(
                source="example",
                source_url="https://example.com/jobs/1",
                title="Example coordinator",
                company="Example",
                location="Toulouse, France",
            )
        )
    finally:
        store.close()

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Export-only must skip scraping and observation pruning")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources"):
        monkeypatch.setattr(cli, name, forbidden)
    monkeypatch.setattr(JobStore, "prune_observations", forbidden)

    result = CliRunner().invoke(
        cli.app,
        [
            "--export-only",
            "--db",
            str(database),
            "--export-dir",
            str(export_dir),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (export_dir / "jobs.xlsx").is_file()
    for name in ("jobs.csv", "jobs_raw.csv"):
        assert "Example coordinator" in (export_dir / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("include_unrecognized", [False, True])
def test_export_only_recalculates_a_changed_radius_from_cached_towns(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    toulouse_scope: LocationScopeConfig,
    include_unrecognized: bool,
) -> None:
    database = tmp_path / "jobs.duckdb"
    prior = SearchConfig(locations=("Toulouse, France",), distance_km=25, paths={"database_path": database})
    search_regions.save_location_scope(prior, toulouse_scope)
    geography = tmp_path / ".geography"
    geography.mkdir()
    center = toulouse_scope.centers[0].model_dump(mode="json")
    (geography / "town-toulouse.json").write_text(
        json.dumps({"center": center, "department": "31", "region_code": "76"}), encoding="utf-8"
    )
    (geography / "region-76.json").write_text(
        json.dumps(
            {
                "name": "Occitanie",
                "municipalities": [
                    {"name": city, "latitude": point[0], "longitude": point[1]}
                    for city, point in toulouse_scope.city_coordinates.items()
                ],
            }
        ),
        encoding="utf-8",
    )
    (geography / "region-11.json").write_text(
        json.dumps(
            {
                "name": "Île-de-France",
                "municipalities": [{"name": "Paris", "latitude": 48.8566, "longitude": 2.3522}],
            }
        ),
        encoding="utf-8",
    )
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        json.dumps(
            {
                "locations": ["Toulouse, France"],
                "distance_km": 5,
                "location_scope": {"include_unrecognized": include_unrecognized},
                "export": {"excel_columns": ["Intitulé", "Lieu", "À vérifier", "Lien"]},
            }
        ),
        encoding="utf-8",
    )
    misspelling = "Toulouze, Occitanie, France"
    store = JobStore(database)
    try:
        for number, (town, title) in enumerate(
            (
                ("Toulouse", "Example coordinator"),
                ("Blagnac", "Example specialist"),
                (misspelling, "Example analyst"),
                ("Paris", "Example manager"),
                ("Toulouze, Île-de-France, France", "Example operator"),
                ("Occitanie", "Example lead"),
            ),
            1,
        ):
            store.upsert_offer(
                JobOffer(
                    source="example",
                    source_url=f"https://example.com/jobs/{number}",
                    title=title,
                    company="Example",
                    location=town,
                    employment_type="CDI",
                )
            )
    finally:
        store.close()

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Export-only must use the municipality cache without fetching")

    monkeypatch.setattr(search_regions.httpx, "Client", forbidden)
    export_dir = tmp_path / "exports"
    args = [
        "--export-only",
        "--profile",
        str(profile),
        "--db",
        str(database),
        "--export-dir",
        str(export_dir),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    result = CliRunner().invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    workbook = load_workbook(export_dir / "jobs.xlsx", read_only=True)
    try:
        rows = list(workbook["Offres"].values)
        location, review = rows[0].index("Lieu"), rows[0].index("À vérifier")
        notes = {row[location]: row[review] for row in rows[1:]}
        expected = {"Toulouse", misspelling} if include_unrecognized else {"Toulouse"}
        assert set(notes) == expected
        if include_unrecognized:
            assert notes[misspelling] == "Localisation à vérifier"
        assert workbook["Données brutes"].max_row == 7
    finally:
        workbook.close()
    assert (misspelling in (export_dir / "jobs.csv").read_text(encoding="utf-8")) is include_unrecognized
    if include_unrecognized:
        settings = json.loads(profile.read_text(encoding="utf-8"))
        settings["location_scope"]["include_unrecognized"] = False
        profile.write_text(json.dumps(settings), encoding="utf-8")
        result = CliRunner().invoke(cli.app, args)
        assert result.exit_code == 0, result.output
        assert misspelling not in (export_dir / "jobs.csv").read_text(encoding="utf-8")
        assert misspelling in (export_dir / "jobs_raw.csv").read_text(encoding="utf-8")


def test_offline_replay_restores_purged_offers_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive_path = tmp_path / "archive.duckdb"
    archive = JobStore(archive_path)
    try:
        offer = JobOffer(
            source="example",
            source_id="1",
            source_url="https://example.com/jobs/1",
            title="Example coordinator",
            company="Example",
            location="Toulouse, France",
            scraped_at="2026-06-04T12:00:00+00:00",
            raw={"listing_id": "1"},
        )
        archive.upsert_offer(offer)
        archive.upsert_offer(offer)
        archive.conn.execute("DELETE FROM raw_offers")
        assert archive.raw_dataframe().empty
        assert archive.conn.execute("SELECT count(*) FROM offer_observations").fetchone() == (1,)
    finally:
        archive.close()

    def forbidden(*args: object) -> Never:
        raise AssertionError("Replay must never scrape")

    for name in ("_run_custom_sources", "_run_all_sources_parallel", "scrape_jobspy_sources"):
        monkeypatch.setattr(cli, name, forbidden)

    target = tmp_path / "rebuilt.duckdb"
    for _ in range(2):
        result = CliRunner().invoke(
            cli.app,
            [
                "--replay-from",
                str(archive_path),
                "--db",
                str(target),
                "--export-dir",
                str(tmp_path / "exports"),
                "--log-file",
                str(tmp_path / "run.jsonl"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "observations_replayed" in result.output
        store = JobStore(target)
        try:
            assert len(store.raw_dataframe()) == len(store.canonical_dataframe()) == 1
            assert store.conn.execute("SELECT count(*) FROM search_state").fetchone() == (0,)
        finally:
            store.close()
