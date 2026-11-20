from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from scrapper.config import SearchConfig
from scrapper.scrapers import wttj
from scrapper.storage import JobStore


def test_wttj_discovers_runtime_config_with_client_default_headers(monkeypatch) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/fr/sitemap.xml"
        assert request.headers["user-agent"].startswith("python-httpx/")
        return httpx.Response(200, text='''<script>window.env = {
            "ALGOLIA_API_KEY_CLIENT": "test-public-key",
            "ALGOLIA_APPLICATION_ID": "test-app",
            "ALGOLIA_JOBS_INDEX_PREFIX": "test-jobs",
            "nested": {"value": "braces } in a string"}
        };</script>''')

    client_class = httpx.AsyncClient
    monkeypatch.setattr(
        wttj.httpx, "AsyncClient",
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
def test_wttj_discovery_rejects_errors_and_incomplete_runtime(monkeypatch, status: int, html: str) -> None:
    client_class = httpx.AsyncClient
    transport = httpx.MockTransport(lambda _: httpx.Response(status, text=html))
    monkeypatch.setattr(wttj.httpx, "AsyncClient", lambda **kwargs: client_class(transport=transport, **kwargs))

    assert asyncio.run(wttj._public_algolia_config(SearchConfig())) is None


def test_wttj_missing_public_algolia_config_records_search_state(monkeypatch, tmp_path: Path) -> None:
    async def missing_public_config(_: SearchConfig) -> None:
        return None

    monkeypatch.setattr(wttj, "_public_algolia_config", missing_public_config)
    config = SearchConfig(queries=("example role",), database_path=tmp_path / "jobs.duckdb")
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))
        row = store.conn.execute(
            """
            SELECT last_seen_count, last_error
            FROM search_state
            WHERE source = ?
            """,
            [config.welcome_to_the_jungle.source],
        ).fetchone()

        assert count == 0
        assert row == (0, "Welcome to the Jungle Algolia runtime config is not available.")
    finally:
        store.close()


def test_disabled_wttj_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    async def unexpected_public_config(_: SearchConfig) -> wttj.WttjAlgoliaConfig | None:
        raise AssertionError("disabled source should not fetch Algolia config")

    monkeypatch.setattr(wttj, "_public_algolia_config", unexpected_public_config)
    config = SearchConfig(
        queries=("example role",),
        enabled_sources={"welcome_to_the_jungle": False},
        database_path=tmp_path / "jobs.duckdb",
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
            [config.welcome_to_the_jungle.source],
        ).fetchone()

        assert count == 0
        assert row == (0,)
    finally:
        store.close()


def test_blank_env_key_falls_back_to_public_algolia_key(monkeypatch, tmp_path: Path) -> None:
    async def fake_public_config(_: SearchConfig) -> wttj.WttjAlgoliaConfig:
        return wttj.WttjAlgoliaConfig(
            api_key="public-key-123",
            app_id="app-id-123",
            url="https://app-id-123-dsn.algolia.net/1/indexes/*/queries",
            job_index="wttj_jobs_production_fr",
        )

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"hits": []}]}

    class DummyClient:
        def __init__(self, *args, **kwargs):
            self.headers = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, content: str) -> DummyResponse:
            assert url == "https://app-id-123-dsn.algolia.net/1/indexes/*/queries"
            assert self.headers["x-algolia-application-id"] == "app-id-123"
            assert self.headers["x-algolia-api-key"] == "public-key-123"
            assert "wttj_jobs_production_fr" in content
            return DummyResponse()

    monkeypatch.setattr(wttj, "_public_algolia_config", fake_public_config)
    monkeypatch.setattr(wttj.httpx, "AsyncClient", DummyClient)

    config = SearchConfig(
        queries=("example role",),
        database_path=tmp_path / "jobs.duckdb",
    )
    monkeypatch.setenv("SCRAPPER_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY", "")
    config = SearchConfig(
        queries=("example role",),
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))
        row = store.conn.execute(
            """
            SELECT last_seen_count, last_error
            FROM search_state
            WHERE source = ?
            """,
            [config.welcome_to_the_jungle.source],
        ).fetchone()

        assert count == 0
        assert row == (0, None)
    finally:
        store.close()


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


def test_wttj_stops_when_hit_is_older_than_custom_window(monkeypatch, tmp_path: Path) -> None:
    async def fake_public_config(_: SearchConfig) -> wttj.WttjAlgoliaConfig:
        return wttj.WttjAlgoliaConfig(
            api_key="public-key-123",
            app_id="app-id-123",
            url="https://app-id-123-dsn.algolia.net/1/indexes/*/queries",
            job_index="wttj_jobs_production_fr",
        )

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {
                "results": [
                    {
                        "hits": [
                            {
                                "objectID": "job-old",
                                "slug": "old-job",
                                "name": "Chargé de communication",
                                "published_at": "2020-01-01T00:00:00Z",
                                "organization": {"slug": "example", "name": "Example"},
                                "office": {"city": "Toulouse", "country": "France"},
                            }
                        ]
                    }
                ]
            }

    class DummyClient:
        calls = 0

        def __init__(self, *args, **kwargs):
            return None

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def post(self, url: str, content: str) -> DummyResponse:
            self.__class__.calls += 1
            return DummyResponse()

    monkeypatch.setattr(wttj, "_public_algolia_config", fake_public_config)
    monkeypatch.setattr(wttj.httpx, "AsyncClient", DummyClient)

    config = SearchConfig(
        queries=("communication",),
        locations=("Toulouse, France",),
        custom_max_pages=3,
        location_scope={"enabled": False},
        database_path=tmp_path / "jobs.duckdb",
    )
    store = JobStore(config.database_path)
    try:
        count = asyncio.run(wttj.scrape_welcome_to_the_jungle(config, store))

        assert count == 0
        assert DummyClient.calls == 1
    finally:
        store.close()
