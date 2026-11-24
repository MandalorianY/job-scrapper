from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Self
from urllib.parse import parse_qs

import httpx
import pytest

from jobsfr.config import SearchConfig
from jobsfr.offers.salary import salary_bounds
from jobsfr.scrapers import wttj
from jobsfr.scrapers.async_http import HostThrottle, SourceBlockedError
from jobsfr.storage import JobStore


def test_wttj_discovers_runtime_config_with_client_default_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fr/sitemap.xml"
        assert request.headers["user-agent"].startswith("python-httpx/")
        return httpx.Response(
            200,
            text="""<script>window.env = {
            "ALGOLIA_API_KEY_CLIENT": "test-public-key",
            "ALGOLIA_APPLICATION_ID": "test-app",
            "ALGOLIA_JOBS_INDEX_PREFIX": "test-jobs",
            "nested": {"value": "braces } in a string"}
        };</script>""",
        )

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        wttj.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    result = asyncio.run(wttj._public_algolia_config(SearchConfig()))

    assert result is not None
    assert result.api_key == "test-public-key"
    assert result.app_id == "test-app"
    assert result.job_index == "test-jobs_fr"
    assert result.url.startswith("https://test-app-dsn.algolia.net/1/indexes/*/queries")


@pytest.mark.parametrize(
    ("status", "html"),
    [(403, "Forbidden"), (202, "challenge"), (200, 'window.env = {"ALGOLIA_APPLICATION_ID":"test"};')],
)
def test_wttj_discovery_rejects_errors_and_incomplete_runtime(
    monkeypatch: pytest.MonkeyPatch, status: int, html: str
) -> None:
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text=html))
    monkeypatch.setattr(wttj.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    assert asyncio.run(wttj._public_algolia_config(SearchConfig())) is None


def _scrape_with_search_state(config: SearchConfig) -> tuple[int, tuple[object, ...] | None]:
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))
        row = store.conn.execute(
            """
            SELECT last_seen_count, last_error
            FROM search_state
            WHERE source = ?
            """,
            [wttj.WTTJ_SOURCE],
        ).fetchone()
        return count, row
    finally:
        store.close()


def test_wttj_missing_public_algolia_config_records_search_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def missing_public_config(_: SearchConfig, throttle: HostThrottle) -> None:
        return None

    monkeypatch.setattr(wttj, "_public_algolia_config", missing_public_config)
    config = SearchConfig(queries=("example role",), paths={"database_path": tmp_path / "jobs.duckdb"})
    assert _scrape_with_search_state(config) == (
        0,
        (0, "Welcome to the Jungle Algolia runtime config is not available."),
    )


def test_wttj_stops_when_runtime_discovery_is_challenged(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        wttj.httpx,
        "AsyncClient",
        lambda **kwargs: client_class(transport=httpx.MockTransport(respond), **kwargs),
    )
    config = SearchConfig(queries=("communication", "marketing"), paths={"database_path": tmp_path / "jobs.duckdb"})
    assert _scrape_with_search_state(config) == (0, (0, SourceBlockedError.message))
    assert requests == ["/fr/sitemap.xml"]


def test_disabled_wttj_source_is_skipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def unexpected_public_config(_: SearchConfig, throttle: HostThrottle) -> wttj.WttjAlgoliaConfig | None:
        raise AssertionError("disabled source should not fetch Algolia config")

    monkeypatch.setattr(wttj, "_public_algolia_config", unexpected_public_config)
    config = SearchConfig(
        queries=("example role",),
        enabled_sources={"welcome_to_the_jungle": False},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))
        row = store.conn.execute(
            """
            SELECT COUNT(*)
            FROM search_state
            WHERE source = ?
            """,
            [wttj.WTTJ_SOURCE],
        ).fetchone()

        assert count == 0
        assert row == (0,)
    finally:
        store.close()


@pytest.fixture
def mock_public_algolia_config(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_public_config(_: SearchConfig, throttle: HostThrottle) -> wttj.WttjAlgoliaConfig:
        return wttj.WttjAlgoliaConfig(
            api_key="public-key-123",
            app_id="app-id-123",
            url="https://app-id-123-dsn.algolia.net/1/indexes/*/queries",
            job_index="wttj_jobs_production_fr",
        )

    monkeypatch.setattr(wttj, "_public_algolia_config", fake_public_config)


def test_blank_env_key_falls_back_to_public_algolia_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mock_public_algolia_config: None
) -> None:
    class DummyClient:
        def __init__(self, *args: object, headers: dict[str, str] | None = None, **kwargs: object) -> None:
            self.headers = headers or {}

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(
            self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
        ) -> None:
            return None

        async def post(self, url: str, content: str) -> httpx.Response:
            assert url == "https://app-id-123-dsn.algolia.net/1/indexes/*/queries"
            assert self.headers["x-algolia-application-id"] == "app-id-123"
            assert self.headers["x-algolia-api-key"] == "public-key-123"
            assert "wttj_jobs_production_fr" in content
            return httpx.Response(200, request=httpx.Request("POST", url), json={"results": [{"hits": []}]})

    monkeypatch.setattr(wttj.httpx, "AsyncClient", DummyClient)

    monkeypatch.setenv("JOBSFR_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY", "")
    config = SearchConfig(
        queries=("example role",),
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    assert _scrape_with_search_state(config) == (0, (0, None))


def test_wttj_hit_uses_known_direct_apply_fields() -> None:
    hit = {
        "objectID": "job-1",
        "slug": "charge-communication",
        "name": "Chargé de communication",
        "application_url": "https://ats.example/apply/1",
        "organization": {"slug": "example", "name": "Example"},
        "office": {"city": "Toulouse", "country": "France"},
    }

    offer = wttj._hit_to_offer(hit, "communication", SearchConfig())

    assert offer.direct_url == "https://ats.example/apply/1"


def test_wttj_keeps_recent_hits_after_old_relevance_results(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mock_public_algolia_config: None
) -> None:
    old_hit = {
        "objectID": "job-old",
        "slug": "old-job",
        "name": "Chargé de communication",
        "published_at": "2020-01-01T00:00:00Z",
        "organization": {"slug": "example", "name": "Example"},
        "office": {"city": "Toulouse", "country": "France"},
    }
    recent_hit = dict(
        old_hit,
        objectID="job-recent",
        slug="recent-job",
        published_at=datetime.now(UTC).isoformat(),
    )
    hits_by_page = ([old_hit], [old_hit, recent_hit], [])
    pages = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert "x-algolia-api-key" not in request.headers
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text=(
                    "<title>Chargé de communication - Example - CDI à Toulouse</title>"
                    '<script type="application/ld+json">{"@type":"JobPosting","description":"Missions complètes.",'
                    '"jobLocationType":"TELECOMMUTE","baseSalary":{"@type":"MonetaryAmount","currency":"EUR",'
                    '"value":{"value":2700,"unitText":"MONTH"}}}</script>'
                ),
            )
        params = parse_qs(json.loads(request.content)["requests"][0]["params"])
        page = int(params["page"][0])
        pages.append(page)
        return httpx.Response(200, json={"results": [{"hits": hits_by_page[page]}]})

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(wttj.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    config = SearchConfig(
        queries=("communication",),
        filters={"title": {"include": ("communication",)}},
        locations=("Toulouse, France",),
        custom_max_pages=3,
        location_scope={"enabled": False},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))

        assert count == 1
        assert pages == [0, 1, 2]
        assert store.raw_dataframe()["source_url"].tolist() == [
            f"{config.welcome_to_the_jungle.site_url}/fr/companies/example/jobs/recent-job",
        ]
        assert store.raw_dataframe()["description"].tolist() == ["Missions complètes."]
        record = store.raw_dataframe().iloc[0]
        assert record["employment_type"] == "CDI"
        assert bool(record["is_remote"])
        assert salary_bounds(record.get) == (32400, 32400)
    finally:
        store.close()


def test_wttj_refreshes_duplicate_listing_once_per_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mock_public_algolia_config: None
) -> None:
    hit = {
        "slug": "charge-communication",
        "name": "Chargé de communication",
        "summary": "Déployer les campagnes de communication.",
        "organization": {"slug": "example", "name": "Example"},
        "office": {"city": "Toulouse", "country": "France"},
    }
    detail_requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            detail_requests.append(request.url.path)
            return httpx.Response(200, text="<html></html>")
        params = parse_qs(json.loads(request.content)["requests"][0]["params"])
        duplicate = dict(hit, objectID=f"{params['query'][0]}-{params['page'][0]}")
        return httpx.Response(200, json={"results": [{"hits": [duplicate]}]})

    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(wttj.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    config = SearchConfig(
        queries=("communication", "chargé de communication"),
        filters={"title": {"include": ("communication",)}},
        custom_max_pages=2,
        http_request_delay_secs=0,
        refresh_known_details=False,
        location_scope={"enabled": False},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    refresh_config = config.model_copy(update={"refresh_known_details": True})
    listing_path = "/fr/companies/example/jobs/charge-communication"

    assert _scrape_with_search_state(refresh_config) == (1, (2, None))
    assert detail_requests == [listing_path]
    assert _scrape_with_search_state(config) == (0, (2, None))
    assert _scrape_with_search_state(refresh_config) == (1, (2, None))
    assert detail_requests == [listing_path, listing_path]
