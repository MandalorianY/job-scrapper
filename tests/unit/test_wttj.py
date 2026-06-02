from __future__ import annotations

import asyncio
from pathlib import Path

from scrapper.config import SearchConfig
import scrapper.scrapers.wttj as wttj
from scrapper.storage import JobStore


def test_wttj_missing_public_algolia_key_records_search_state(monkeypatch, tmp_path: Path) -> None:
    async def missing_public_key(_: SearchConfig) -> None:
        return None

    monkeypatch.setattr(wttj, "_public_algolia_api_key", missing_public_key)
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
        assert row == (0, "Welcome to the Jungle Algolia API key is not configured.")
    finally:
        store.close()


def test_disabled_wttj_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    async def unexpected_public_key(_: SearchConfig) -> str | None:
        raise AssertionError("disabled source should not fetch an API key")

    monkeypatch.setattr(wttj, "_public_algolia_api_key", unexpected_public_key)
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
    async def fake_public_key(_: SearchConfig) -> str | None:
        return "public-key-123"

    class DummyResponse:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"results": [{"hits": []}]}

    class DummyClient:
        def __init__(self, *args, **kwargs):  # noqa: ARG002
            self.headers = kwargs.get("headers", {})

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):  # noqa: ARG002
            return None

        async def post(self, url: str, content: str) -> DummyResponse:  # noqa: ARG002
            assert self.headers["x-algolia-api-key"] == "public-key-123"
            return DummyResponse()

    monkeypatch.setattr(wttj, "_public_algolia_api_key", fake_public_key)
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
        "office": {"city": "Lyon", "country": "France"},
    }

    offer = wttj._hit_to_offer(hit, "communication", SearchConfig())

    assert offer.direct_url == "https://ats.example/apply/1"
