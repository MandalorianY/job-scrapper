from pathlib import Path
from typing import Never

import httpx
import pytest
from typer.testing import CliRunner

from scrapper import catalog_updates, cli
from scrapper.company_types import bundled_company_catalog
from scrapper.config import SearchConfig
from scrapper.storage import JobStore

CSV = "company,type,sources\nExample Portage,portage_salarial,https://example.test\n"


def test_catalog_refresh_reuses_last_valid_snapshot(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = 100_000.0
    requested_etags: list[str | None] = []
    responses = iter(
        (
            (200, CSV, '"abc"'),
            (304, "", '"abc"'),
            (200, CSV.replace("Example Portage", "New Portage"), '"def"'),
            (200, "bad CSV", '"bad"'),
        )
    )

    def get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        assert url == catalog_updates.CATALOG_URL
        requested_etags.append(headers.get("If-None-Match"))
        status, content, etag = next(responses)
        return httpx.Response(status, text=content, headers={"ETag": etag}, request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_updates, "time", lambda: now)
    monkeypatch.setattr(catalog_updates.httpx, "get", get)
    database = tmp_path / "jobs.duckdb"
    catalog = catalog_updates.resolve_company_catalog(database, None, refresh=True)
    assert catalog.types == {"portage_salarial": frozenset({"example portage"})}
    assert catalog_updates.resolve_company_catalog(database, None, refresh=True) == catalog
    assert catalog_updates.resolve_company_catalog(database, None, refresh=False) == catalog
    now += 86_400
    assert catalog_updates.resolve_company_catalog(database, None, refresh=True) == catalog
    now += 86_400
    updated = catalog_updates.resolve_company_catalog(database, None, refresh=True)
    assert updated.types == {"portage_salarial": frozenset({"new portage"})}
    assert updated.revision != catalog.revision
    now += 86_400
    assert catalog_updates.resolve_company_catalog(database, None, refresh=True) == updated
    assert catalog_updates.resolve_company_catalog(database, None, refresh=False) == updated
    assert requested_etags == [None, '"abc"', '"abc"', '"def"']


def test_unavailable_catalog_uses_bundled_snapshot_and_backs_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    requests = 0

    def get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(503, request=httpx.Request("GET", url))

    monkeypatch.setattr(catalog_updates.httpx, "get", get)
    database = tmp_path / "jobs.duckdb"
    assert catalog_updates.resolve_company_catalog(database, None, refresh=False) == bundled_company_catalog()
    assert requests == 0
    for _ in range(2):
        assert catalog_updates.resolve_company_catalog(database, None, refresh=True) == bundled_company_catalog()
    assert requests == 1


def test_pinned_catalog_stays_local(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Pinned catalogs must be loaded locally")

    monkeypatch.setattr(catalog_updates.httpx, "get", forbidden)
    pinned = tmp_path / "catalog.csv"
    pinned.write_text(CSV, encoding="utf-8")
    config = SearchConfig(
        _refresh_company_catalog=True,
        company_catalog_path=pinned,
        filters={"company": {"exclude": {"types": ["portage_salarial"]}}},
    )
    assert not config.filters.company.allows("Example Portage SAS")
    assert config.company_catalog_revision


def test_online_cli_refreshes_before_validating_types(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def get(url: str, *, headers: dict[str, str], timeout: float) -> httpx.Response:
        return httpx.Response(200, text=CSV, request=httpx.Request("GET", url))

    def scrape(config: SearchConfig, store: JobStore, *, progress: cli.ProgressReporter) -> dict[str, int]:
        offer = {"title": "Example coordinator", "company": "Example Portage", "source": "indeed"}
        assert not config.filters.allows(offer.get)
        offer["source"] = "linkedin"
        assert config.filters.allows(offer.get)
        assert config.company_catalog_revision
        return {}

    monkeypatch.setattr(catalog_updates.httpx, "get", get)
    monkeypatch.setattr(cli, "route_location_overrides", lambda config, **kwargs: config)
    monkeypatch.setattr(cli, "scrape_jobspy_sources", scrape)
    profile = tmp_path / "profile.yaml"
    profile.write_text(
        "location_scope: {enabled: false}\n"
        "filters: {by_source: {indeed: {company: {exclude: {types: [portage_salarial]}}}}}\n",
        encoding="utf-8",
    )
    args = [
        "--profile",
        str(profile),
        "--db",
        str(tmp_path / "jobs.duckdb"),
        "--export-dir",
        str(tmp_path / "exports"),
        "--log-file",
        str(tmp_path / "jobs.jsonl"),
    ]
    result = CliRunner().invoke(cli.app, [*args, "--skip-custom"])
    assert result.exit_code == 0, result.output

    def forbidden(*args: object, **kwargs: object) -> Never:
        raise AssertionError("Offline commands must not fetch a catalog")

    monkeypatch.setattr(catalog_updates.httpx, "get", forbidden)
    for flag in ("--export-only", "--show-config", "--status"):
        result = CliRunner().invoke(cli.app, [*args, flag])
        assert result.exit_code == 0, result.output
