from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from jobsfr.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from jobsfr.offers.models import JobOffer
from jobsfr.scrapers import async_http, cadremploi
from jobsfr.scrapers.aggregators import discovery, scrape, scrape_aggregators
from jobsfr.scrapers.async_http import HostThrottle, SourceBlockedError
from jobsfr.storage import JobStore


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_source", [False, True])
async def test_aggregator_groups_share_limits_and_overlap_when_disjoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shared_source: bool
) -> None:
    config = SearchConfig()
    direct_source = "isarta" if shared_source else "jobijoba"
    config.aggregators.direct_specs = (config.aggregators.direct_specs[0].model_copy(update={"source": direct_source}),)
    config.aggregators.site_search_sources = tuple(
        spec for spec in config.aggregators.site_search_sources if spec.source == "isarta"
    )
    site_started = asyncio.Event()
    direct_finished = asyncio.Event()
    resources: list[tuple[httpx.AsyncClient, HostThrottle]] = []

    async def direct(
        client: httpx.AsyncClient,
        spec: DirectAggregatorConfig,
        config: SearchConfig,
        store: JobStore,
        throttle: HostThrottle,
    ) -> int:
        resources.append((client, throttle))
        if not shared_source:
            await asyncio.wait_for(site_started.wait(), timeout=1)
        direct_finished.set()
        return 1

    async def site(
        client: httpx.AsyncClient, config: SearchConfig, store: JobStore, throttle: HostThrottle
    ) -> dict[str, int]:
        resources.append((client, throttle))
        assert direct_finished.is_set() is shared_source
        site_started.set()
        return {"isarta": 2}

    monkeypatch.setattr(scrape, "_scrape_direct_spec", direct)
    monkeypatch.setattr(scrape, "_scrape_site_search", site)
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert await scrape_aggregators(config, store) == {direct_source: 1, "isarta": 2}
    finally:
        store.close()
    assert len(resources) == 2 and resources[0] == resources[1]


def test_isarta_discovers_absolute_job_query_links() -> None:
    html = """
        <a href="https://isarta.fr/?job=123"><img src="logo.png"></a>
        <a href="https://isarta.fr/?job=123">Chargé de communication Toulouse</a>
        <a href="https://isarta.fr/?job=123">Publié le 16 octobre</a>
        <a href="https://isarta.fr/?job=456">Développeur</a>
    """

    async def run() -> list[str]:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await discovery._direct_site_search_results(
                client,
                "isarta",
                "communication",
                SearchConfig(filters={"title": {"include": ("communication",)}}),
                ("https://isarta.fr/cgi-bin/emplois/jobs?cat=occitanie&query={query}",),
            )

    assert asyncio.run(run()) == ["https://isarta.fr/?job=123"]


@pytest.mark.asyncio
async def test_isarta_reuses_detail_with_each_search_watermark(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    config = SearchConfig(
        queries=("communication", "marketing"),
        locations=("Toulouse, France",),
        filters={"title": {"include": ("communication",)}},
        location_scope={"enabled": False},
        overlap_hours=24,
        default_hours_old=336,
    )
    isarta = next(spec for spec in config.aggregators.site_search_sources if spec.source == "isarta").model_copy(
        update={"search_url_templates": ("https://isarta.fr/search?q={query}",)}
    )
    config.aggregators.site_search_sources = (isarta,)
    job = {
        "@type": "JobPosting",
        "title": "Communication",
        "description": "Concevoir les campagnes.",
        "datePosted": (now - timedelta(days=3)).date().isoformat(),
    }
    detail_calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal detail_calls
        if request.url.path == "/search":
            html = '<a href="/?job=123">Communication</a>'
        else:
            assert request.url.params["login"] == "123"
            detail_calls += 1
            html = f'<script type="application/ld+json">{json.dumps(job)}</script>'
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    first_query, second_query = (
        isarta.query_template.format(query=query, location=config.search_locations[0]) for query in config.queries
    )
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.start_search("isarta", first_query, config.search_locations[0])
        store.finish_search("isarta", first_query, config.search_locations[0], 0)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await scrape._scrape_site_search(client, config, store) == {"isarta": 1}
        assert store.conn.execute("SELECT search_query, search_location FROM raw_offers").fetchall() == [
            (second_query, config.search_locations[0])
        ]
        assert store.conn.execute("SELECT count(*) FROM source_health WHERE status = 'succeeded'").fetchone() == (2,)
    finally:
        store.close()
    assert detail_calls == 1


def test_cadremploi_all_blocked_pages_raise_instead_of_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url)))

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            config = SearchConfig(
                locations=("Toulouse, France",),
                aggregators={"site_locations": {"Toulouse, France": {"department": "31", "region_slug": "occitanie"}}},
            )
            return await discovery._direct_site_search_results(client, "cadremploi", "communication", config)

    with pytest.raises(RuntimeError, match="cadremploi discovery failed: HTTP 403"):
        asyncio.run(run())


def test_cadremploi_continues_to_working_location_after_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url)))

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("ville") != "nantes-44":
            return httpx.Response(403)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=('<a href="/emploi/detail_offre?annonceId=123">Chargé de communication</a>'),
        )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await discovery._direct_site_search_results(
                client,
                "cadremploi",
                "communication",
                SearchConfig(filters={"title": {"include": ("communication",)}}),
                (
                    "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=toulouse-31",
                    "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=nantes-44",
                ),
            )

    assert asyncio.run(run()) == ["https://www.cadremploi.fr/emploi/detail_offre?annonceId=123"]


@pytest.mark.asyncio
async def test_makesense_navigation_does_not_consume_detail_limit(
    mock_html_client: Callable[[str], httpx.AsyncClient],
) -> None:
    config = SearchConfig()
    spec = next(s for s in config.aggregators.direct_specs if s.source == "makesense").model_copy(
        update={"max_links": 1}
    )
    html = """
        <a href="/fr/s/jobs/all/all">All jobs</a>
        <a href="/fr/s/jobs/all/all/stage">Internships</a>
        <a href="/fr/jobs/employer-communication-123">Chargé de communication</a>
    """
    async with mock_html_client(html) as client:
        links = await discovery._detail_links(client, spec, "https://jobs.makesense.org/fr/s/jobs/all/all")

    assert links == ["https://jobs.makesense.org/fr/jobs/employer-communication-123"]


def test_provider_search_urls_honor_title_and_requested_radius() -> None:
    config = SearchConfig(distance_km=10, aggregators={"direct_locations": ["76R"]})
    specs = {spec.source: spec for spec in config.aggregators.direct_specs}

    france_travail = discovery._search_urls(specs["france_travail"], "chargé communication", config)
    assert france_travail and all("rayon=10" in url for url in france_travail)
    makesense = specs["makesense"].model_copy(
        update={
            "search_url_templates": ("https://jobs.makesense.org/fr/s/jobs/all/communication/cdi?sortBy=relevance",)
        }
    )
    assert discovery._search_urls(makesense, "responsable marketing", config)[0].endswith(
        "?sortBy=relevance&s=responsable+marketing"
    )


def test_site_search_attempts_every_query_and_skips_disabled_sources(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def fake_site_search_results(
        client: httpx.AsyncClient,
        spec: SiteSearchConfig,
        query: str,
        config: SearchConfig,
        throttle: HostThrottle | None,
    ) -> list[str]:
        return [f"https://{spec.host}/jobs/example"]

    async def fake_extract_detail_offer(
        client: httpx.AsyncClient,
        url: str,
        source: str,
        query: str,
        config: SearchConfig,
        **_kwargs: object,
    ) -> None:
        return None

    monkeypatch.setattr(scrape, "_site_search_results", fake_site_search_results)
    monkeypatch.setattr(scrape, "_extract_detail_offer", fake_extract_detail_offer)

    config = SearchConfig(
        queries=("example role", "sample coordinator", "demo lead", "placeholder specialist"),
        locations=("Toulouse, France",),
        enabled_sources={"apec": False, "emploi_collectivites": False},
        paths={"database_path": tmp_path / "jobs.duckdb"},
    )
    store = JobStore(config.database_path)
    headers = {"user-agent": config.user_agent, "accept-language": config.http.accept_language}

    async def run_test() -> dict[str, int]:
        async with httpx.AsyncClient(headers=headers, timeout=30, follow_redirects=True) as client:
            return await scrape._scrape_site_search(client, config, store)

    try:
        counts = asyncio.run(run_test())
        row = store.conn.execute("SELECT COUNT(*) FROM search_state WHERE source = 'apec'").fetchone()

        assert "apec" not in counts
        assert row == (0,)
        assert store.conn.execute("SELECT COUNT(*) FROM search_state WHERE source = 'cadremploi'").fetchone() == (4,)
    finally:
        store.close()


@pytest.mark.asyncio
async def test_collectivites_reuses_successful_sitemap_across_searches(tmp_path: Path) -> None:
    config = SearchConfig(queries=("communication", "marketing"), locations=("Toulouse, France", "Nantes, France"))
    config.aggregators.site_search_sources = tuple(
        spec for spec in config.aggregators.site_search_sources if spec.source == "emploi_collectivites"
    )
    sitemap_requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        sitemap_requests.append(str(request.url))
        return httpx.Response(200, text="<urlset/>")

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await scrape._scrape_site_search(client, config, store)
        assert store.conn.execute("SELECT COUNT(*) FROM source_health WHERE status = 'succeeded'").fetchone() == (4,)
    finally:
        store.close()

    assert sitemap_requests == [discovery.EMPLOI_COLLECTIVITES_SITEMAP]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "retry_after", "detail_requests"), [(429, "60", 1), (503, "0", 2)])
async def test_rate_limited_details_keep_previous_search_watermark(
    tmp_path: Path, status: int, retry_after: str, detail_requests: int
) -> None:
    source = "makesense"
    query = "communication"
    config = SearchConfig(queries=(query,), filters={"title": {"include": (query,)}})
    spec = next(spec for spec in config.aggregators.direct_specs if spec.source == source)
    requested_details = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal requested_details
        if request.url.path == "/fr/jobs/employer-communication-123":
            requested_details += 1
            return httpx.Response(status, headers={"retry-after": retry_after})
        return httpx.Response(200, text='<a href="/fr/jobs/employer-communication-123">Communication</a>')

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.start_search(source, query, config.location_label)
        store.finish_search(source, query, config.location_label, 1)
        watermark = store.get_last_success(source, query, config.location_label)
        assert watermark is not None
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await scrape._scrape_direct_spec(client, spec, config, store, HostThrottle(1, 0)) == 0
        assert store.get_last_success(source, query, config.location_label) == watermark
        row = store.conn.execute("SELECT status, error FROM source_health").fetchone()
        assert row is not None and row[0] == "failed" and str(status) in row[1]
    finally:
        store.close()

    assert requested_details == detail_requests


@pytest.mark.asyncio
async def test_custom_source_stops_queued_details_and_keeps_completed_offers(tmp_path: Path) -> None:
    config = SearchConfig(
        queries=("communication", "marketing"),
        locations=("Toulouse, France", "Nantes, France"),
        filters={"title": {"include": ("communication",)}},
        location_scope={"enabled": False},
        aggregators={
            "site_search_sources": {
                "isarta": {"search_url_templates": ("https://isarta.fr/search?q={query}",)},
                "cadremploi": {"search_url_templates": ("https://www.cadremploi.fr/search?q={query}",)},
            },
            "max_detail_pages": 3,
        },
        enabled_sources={"apec": False, "optioncarriere": False, "emploi_collectivites": False},
    )
    config.aggregators.site_search_sources = tuple(reversed(config.aggregators.site_search_sources))
    requests: list[tuple[str, str, str | None]] = []
    cadremploi_started = asyncio.Event()
    isarta_started = asyncio.Event()

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host, request.url.path, request.url.params.get("login")))
        if request.url.host == "www.cadremploi.fr":
            cadremploi_started.set()
            await asyncio.wait_for(isarta_started.wait(), timeout=1)
            if request.url.params["q"] == "communication":
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, headers={"content-type": "text/html"})
        if request.url.path == "/search":
            isarta_started.set()
            await asyncio.wait_for(cadremploi_started.wait(), timeout=1)
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text="".join(f'<a href="/?job={i}">Communication</a>' for i in range(1, 4)),
            )
        if request.url.params["login"] == "1":
            return httpx.Response(
                200,
                headers={"content-type": "text/html"},
                text='<script type="application/ld+json">{"@type":"JobPosting","title":"Communication",'
                '"hiringOrganization":{"name":"Sample employer"},"description":"A job in Toulouse."}</script>',
            )
        return httpx.Response(403, headers={"cf-mitigated": "challenge"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        store = JobStore(tmp_path / "jobs.duckdb")
        try:
            await scrape._scrape_site_search(client, config, store, HostThrottle(1, 0))
            assert store.sources_with_search_error(SourceBlockedError.message) == ["isarta"]
            assert store.conn.execute("SELECT COUNT(*) FROM raw_offers WHERE source = 'isarta'").fetchone() == (1,)
            assert store.conn.execute(
                "SELECT source, count(*), count(error), count(last_success_at) FROM source_health GROUP BY source ORDER BY source"
            ).fetchall() == [("cadremploi", 4, 2, 2), ("isarta", 1, 1, 0)]
        finally:
            store.close()

    assert [request for request in requests if request[0] == "isarta.fr"] == [
        ("isarta.fr", "/search", None),
        ("isarta.fr", "/cgi-bin/emplois/pm.cgi", "1"),
        ("isarta.fr", "/cgi-bin/emplois/pm.cgi", "2"),
    ]


@pytest.mark.asyncio
async def test_direct_batches_stop_on_challenge_and_save_completed_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = SearchConfig(queries=("communication", "marketing"), location_scope={"enabled": False})
    spec = next(spec for spec in config.aggregators.direct_specs if spec.source == "makesense")
    urls = [f"https://example.org/jobs/{number}" for number in range(3)]
    started: list[str] = []
    second_started = asyncio.Event()

    async def links(*args: object, **kwargs: object) -> list[str]:
        return urls

    async def extract(
        _client: httpx.AsyncClient, url: str, source: str, _query: str, _config: SearchConfig, **kwargs: object
    ) -> JobOffer:
        started.append(url)
        if url == urls[1]:
            second_started.set()
            raise SourceBlockedError()
        await asyncio.wait_for(second_started.wait(), timeout=1)
        return JobOffer(
            source=source,
            source_url=url,
            title="Chargé de communication",
            company="Example employer",
            employment_type="CDI",
        )

    monkeypatch.setattr(scrape, "_detail_links", links)
    monkeypatch.setattr(scrape, "_extract_detail_offer", extract)
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        async with httpx.AsyncClient() as client:
            assert await scrape._scrape_direct_spec(client, spec, config, store, HostThrottle(2, 0)) == 1
        assert started == urls[:2]
        assert store.sources_with_search_error(SourceBlockedError.message) == [spec.source]
        assert store.raw_dataframe()["source_url"].tolist() == urls[:1]
    finally:
        store.close()


@pytest.mark.asyncio
async def test_custom_source_records_blank_timeout_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(async_http, "_DEFAULT_RETRY_DELAY", 0)
    config = SearchConfig(queries=("communication", "marketing"))
    spec = next(spec for spec in config.aggregators.direct_specs if spec.source == "makesense")
    queries: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        query = request.url.params["s"]
        queries.append(query)
        if query == "communication":
            raise httpx.ReadTimeout("", request=request)
        return httpx.Response(200, text="<html></html>")

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            await scrape._scrape_direct_spec(client, spec, config, store)
        assert store.conn.execute("SELECT query, error FROM source_health ORDER BY query").fetchall() == [
            ("communication", "ReadTimeout"),
            ("marketing", None),
        ]
    finally:
        store.close()
    assert queries == ["communication", "communication", "marketing"]


def test_site_search_records_missing_direct_route(tmp_path: Path) -> None:
    isarta = next(spec for spec in SearchConfig().aggregators.site_search_sources if spec.source == "isarta")
    config = SearchConfig(
        queries=("example role",),
        locations=("Unconfigured City, France",),
        aggregators={"site_search_sources": (isarta,)},
    )
    store = JobStore(tmp_path / "jobs.duckdb")
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url)
        return httpx.Response(500)

    async def run() -> dict[str, int]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await scrape._scrape_site_search(client, config, store)

    try:
        assert asyncio.run(run()) == {"isarta": 0}
        assert not requests
        assert store.conn.execute("SELECT last_error FROM search_state").fetchone() == (
            "No direct discovery URL configured for isarta",
        )
    finally:
        store.close()
