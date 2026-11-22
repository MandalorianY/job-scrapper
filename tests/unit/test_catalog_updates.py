from pathlib import Path

import httpx
import pytest

from scrapper import catalog_updates
from scrapper.company_types import bundled_company_catalog

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
