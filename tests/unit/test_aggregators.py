from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from scrapper.config import DirectAggregatorConfig, SearchConfig, SiteSearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers import aggregators, async_http
from scrapper.scrapers.async_http import HostThrottle, SourceBlockedError
from scrapper.storage import JobStore


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
        client: httpx.AsyncClient, spec: DirectAggregatorConfig, config: SearchConfig, store: JobStore, throttle: HostThrottle
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

    monkeypatch.setattr(aggregators, "_scrape_direct_spec", direct)
    monkeypatch.setattr(aggregators, "_scrape_site_search", site)
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        assert await aggregators.scrape_aggregators(config, store) == {direct_source: 1, "isarta": 2}
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
            return await aggregators._direct_site_search_results(
                client,
                "isarta",
                "communication",
                SearchConfig(include_terms=("communication",)),
                ("https://isarta.fr/cgi-bin/emplois/jobs?cat=occitanie&query={query}",),
            )

    assert asyncio.run(run()) == ["https://isarta.fr/?job=123"]


@pytest.mark.asyncio
async def test_isarta_reuses_detail_with_each_search_watermark(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    config = SearchConfig(
        queries=("communication", "marketing"),
        locations=("Toulouse, France",),
        include_terms=("communication",),
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
            assert await aggregators._scrape_site_search(client, config, store) == {"isarta": 1}
        assert store.conn.execute("SELECT search_query, search_location FROM raw_offers").fetchall() == [
            (second_query, config.search_locations[0])
        ]
        assert store.conn.execute("SELECT count(*) FROM source_health WHERE status = 'succeeded'").fetchone() == (2,)
    finally:
        store.close()
    assert detail_calls == 1


def test_cadremploi_all_blocked_pages_raise_instead_of_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url))
    )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            config = SearchConfig(
                locations=("Toulouse, France",),
                aggregators={"site_locations": {"Toulouse, France": {"department": "31", "region_slug": "occitanie"}}},
            )
            return await aggregators._direct_site_search_results(client, "cadremploi", "communication", config)

    with pytest.raises(RuntimeError, match="cadremploi discovery failed: HTTP 403"):
        asyncio.run(run())


def test_cadremploi_continues_to_working_location_after_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url))
    )

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
            return await aggregators._direct_site_search_results(
                client,
                "cadremploi",
                "communication",
                SearchConfig(include_terms=("communication",)),
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
        links = await aggregators._detail_links(client, spec, "https://jobs.makesense.org/fr/s/jobs/all/all")

    assert links == ["https://jobs.makesense.org/fr/jobs/employer-communication-123"]


def test_blocked_detail_page_does_not_become_unverified_offer() -> None:
    async def run() -> JobOffer | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            return await aggregators._extract_detail_offer(
                client,
                "https://isarta.fr/?job=123",
                "isarta",
                "communication",
                SearchConfig(),
            )

    assert asyncio.run(run()) is None


def test_provider_search_urls_honor_title_and_requested_radius() -> None:
    config = SearchConfig(distance_km=10, aggregators={"direct_locations": ["76R"]})
    specs = {spec.source: spec for spec in config.aggregators.direct_specs}

    france_travail = aggregators._search_urls(specs["france_travail"], "chargé communication", config)
    assert france_travail and all("rayon=10" in url for url in france_travail)
    makesense = specs["makesense"].model_copy(
        update={
            "search_url_templates": ("https://jobs.makesense.org/fr/s/jobs/all/communication/cdi?sortBy=relevance",)
        }
    )
    assert aggregators._search_urls(makesense, "responsable marketing", config)[0].endswith(
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

    monkeypatch.setattr(aggregators, "_site_search_results", fake_site_search_results)
    monkeypatch.setattr(aggregators, "_extract_detail_offer", fake_extract_detail_offer)

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
            return await aggregators._scrape_site_search(client, config, store)

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
            await aggregators._scrape_site_search(client, config, store)
        assert store.conn.execute("SELECT COUNT(*) FROM source_health WHERE status = 'succeeded'").fetchone() == (4,)
    finally:
        store.close()

    assert sitemap_requests == [aggregators.EMPLOI_COLLECTIVITES_SITEMAP]


@pytest.mark.asyncio
@pytest.mark.parametrize(("status", "retry_after", "detail_requests"), [(429, "60", 1), (503, "0", 2)])
async def test_rate_limited_details_keep_previous_search_watermark(
    tmp_path: Path, status: int, retry_after: str, detail_requests: int
) -> None:
    source = "makesense"
    query = "communication"
    config = SearchConfig(queries=(query,), include_terms=(query,))
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
            assert await aggregators._scrape_direct_spec(client, spec, config, store, HostThrottle(1, 0)) == 0
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
        include_terms=("communication",),
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

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.host, request.url.path, request.url.params.get("login")))
        if request.url.host == "www.cadremploi.fr":
            if request.url.params["q"] == "communication":
                return httpx.Response(503, headers={"retry-after": "0"})
            return httpx.Response(200, headers={"content-type": "text/html"})
        if request.url.path == "/search":
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
            await aggregators._scrape_site_search(client, config, store, HostThrottle(1, 0))
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
    assert requests[3][0] == "www.cadremploi.fr"


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
            await aggregators._scrape_direct_spec(client, spec, config, store)
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
            return await aggregators._scrape_site_search(client, config, store)

    try:
        assert asyncio.run(run()) == {"isarta": 0}
        assert not requests
        assert store.conn.execute("SELECT last_error FROM search_state").fetchone() == (
            "No direct discovery URL configured for isarta",
        )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_jobijoba_detail_parser_rejects_alternance_offer(
    jobijoba_alternance_page: tuple[str, str], mock_html_client: Callable[[str], httpx.AsyncClient]
) -> None:
    url, html = jobijoba_alternance_page
    config = SearchConfig()
    async with mock_html_client(html) as client:
        offer = await aggregators._extract_detail_offer(client, url, "jobijoba", "communication", config)

    assert offer is not None
    assert offer.location == "Castelginest"
    assert offer.employment_type == "Alternance"
    assert offer.date_posted == "Il y a 10 h"
    assert "alternant(e)" in (offer.description or "")
    assert not aggregators._offer_is_usable(offer, SearchConfig())


def test_generic_date_inference_keeps_only_the_date_phrase() -> None:
    assert aggregators._infer_date("Publié le 04 juin 2026 Descriptif de l'offre") == "04 juin 2026"
    assert aggregators._infer_date("Publiée le Il y a 10 h Description de l'offre") == "Il y a 10 h"


def test_meteojob_placeholder_page_is_rejected() -> None:
    config = SearchConfig(include_terms=("communication",), location_scope={"enabled": False})
    offer = JobOffer(
        source="meteojob",
        source_url="https://www.meteojob.com/jobs/55275233",
        direct_url=(
            "https://www.meteojob.com/candidat/offres/"
            "offre-d-emploi-charge-de-communication-h-f-seilh-auvergne-rhone-alpes-secteur-associatif-benevolat-48193251"
        ),
        title="Emploi Chargé de communication à Cugnaux",
        company="Meteojob",
        location="Cugnaux",
        date_posted=None,
        raw={"json_ld": {}, "url": "https://www.meteojob.com/jobs/55275233"},
    )

    assert not aggregators._offer_is_usable(offer, config)


def test_specific_undated_meteojob_offer_is_not_rejected_as_placeholder() -> None:
    config = SearchConfig(include_terms=("communication",), location_scope={"enabled": False})
    offer = JobOffer(
        source="meteojob",
        source_url="https://www.meteojob.com/jobs/example",
        title="Chargé de communication",
        company="Example",
        location="Toulouse",
        description="Poste de communication basé à Toulouse.",
        date_posted=None,
        raw={"json_ld": {}, "url": "https://www.meteojob.com/jobs/example"},
    )

    assert aggregators._offer_is_usable(offer, config)


@pytest.mark.asyncio
async def test_detail_parser_extracts_direct_apply_url(mock_html_client: Callable[[str], httpx.AsyncClient]) -> None:
    url = "https://www.meteojob.com/jobs/example"
    html = """
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@type": "JobPosting",
          "title": "Chargé de communication",
          "hiringOrganization": {"name": "Example"},
          "jobLocation": {"address": {"addressLocality": "Toulouse"}},
          "description": "Poste de communication basé à Toulouse.",
          "employmentType": "CDI"
        }
        </script>
      </head>
      <body>
        <a href="https://www.linkedin.com/shareArticle?url=https://www.meteojob.com/jobs/example">Partager</a>
        <a href="https://ats.example/apply/example">Postuler</a>
      </body>
    </html>
    """
    async with mock_html_client(html) as client:
        offer = await aggregators._extract_detail_offer(client, url, "meteojob", "communication", SearchConfig())

    assert offer is not None
    assert offer.direct_url == "https://ats.example/apply/example"


@pytest.mark.asyncio
async def test_emploi_territorial_detail_parser_uses_source_specific_title(
    emploi_territorial_page: tuple[str, str], mock_html_client: Callable[[str], httpx.AsyncClient]
) -> None:
    url, html = emploi_territorial_page
    config = SearchConfig(
        include_terms=("communication",),
        exclude_terms=("stage", "alternance", "apprentissage"),
        location_scope={"enabled": False},
    )
    async with mock_html_client(html) as client:
        offer = await aggregators._extract_detail_offer(client, url, "emploi_territorial", "communication", config)

    assert offer is not None
    assert offer.title == "Chargé de communication"
    assert offer.company == "MAIRIE DE TOULOUSE"
    assert offer.location == "Toulouse (Haute-Garonne (31))"
    assert offer.employment_type == "Emploi permanent - vacance d'emploi"
    assert aggregators._offer_is_usable(offer, config)


@pytest.fixture
def jobijoba_redirect_page() -> tuple[str, str, str]:
    detail_url = "https://www.jobijoba.com/fr/annonce/54/example"
    redirect_url = "https://www.jobijoba.com/fr/redirect/offer/54/example"
    detail_html = """
    <html>
      <body>
        <div class="offer current">
          <div class="row permalink-open">
            <h1 class="permalink-title">Chargé de communication H/F</h1>
            <span>Toulouse</span>
            <span>CDI</span>
            <span>Example</span>
          </div>
          <p class="permalink-description">Poste de communication basé à Toulouse.</p>
          <span class="button-primary" data-redirect="/fr/redirect/offer/54/example">Postuler</span>
        </div>
      </body>
    </html>
    """
    return detail_url, redirect_url, detail_html


@pytest.mark.parametrize("with_application_link", [False, True])
def test_jobijoba_detail_parser_resolves_redirect_target(
    jobijoba_redirect_page: tuple[str, str, str], with_application_link: bool
) -> None:
    detail_url, redirect_url, detail_html = jobijoba_redirect_page
    job = {"@type": "JobPosting", "title": "Chargé de communication H/F", "description": "Concevoir les campagnes."}
    detail_html = detail_html.replace("</body>", f'<script type="application/ld+json">{json.dumps(job)}</script></body>')
    target_url = "https://www.hellowork.com/fr-fr/emplois/79739983.html?utm_source=jobijoba"
    application_link = f'<a href="https://r.holeest.com/redirect?from={target_url}">Continuer</a>'
    redirect_html = f"""
    <html><body>
      <a href="https://www.googletagmanager.com/gtm.js?id=example">Analytics</a>
      <a href="https://example.com/unrelated">Recommended job</a>
      {application_link if with_application_link else ""}
    </body></html>
    """

    async def run_test() -> JobOffer | None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == redirect_url:
                return httpx.Response(200, text=redirect_html, headers={"content-type": "text/html"}, request=request)
            return httpx.Response(200, text=detail_html, headers={"content-type": "text/html"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(
                client,
                detail_url,
                "jobijoba",
                "communication",
                SearchConfig(include_terms=("communication",), location_scope={"enabled": False}),
                cutoff=datetime(2001, 1, 1, tzinfo=UTC),
            )

    offer = asyncio.run(run_test())

    assert offer is not None
    assert offer.description == "Concevoir les campagnes."
    assert offer.direct_url == (target_url if with_application_link else redirect_url)
    if with_application_link:
        assert offer.source_id == "www.hellowork.com/fr-fr/emplois/79739983.html"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        {"datePosted": "2000-01-01"},
        {"title": "Développeur"},
        {"description": "Offre de stage en communication."},
    ],
)
async def test_jobijoba_skips_redirect_for_filtered_offer(
    tmp_path: Path, jobijoba_redirect_page: tuple[str, str, str], fields: dict[str, str]
) -> None:
    detail_url, _, detail_html = jobijoba_redirect_page
    search_url = "https://www.jobijoba.com/fr/emploi/communication"
    config = SearchConfig(
        queries=("communication",),
        include_terms=("communication",),
        exclude_terms=("stage",),
        location_scope={"enabled": False},
    )
    spec = next(spec for spec in config.aggregators.direct_specs if spec.source == "jobijoba").model_copy(
        update={"search_url_templates": ("https://www.jobijoba.com/fr/emploi/{query}",)}
    )
    job = {"@type": "JobPosting", "title": "Communication", "description": "Concevoir les campagnes."} | fields
    detail_html = detail_html.replace("</body>", f'<script type="application/ld+json">{json.dumps(job)}</script></body>')
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == search_url:
            html = f'<a href="{detail_url}">Communication</a>'
        else:
            assert url == detail_url
            html = detail_html
        return httpx.Response(200, text=html, headers={"content-type": "text/html"})

    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            assert await aggregators._scrape_direct_spec(client, spec, config, store) == 0
    finally:
        store.close()
    assert requests == [search_url, detail_url]


def test_jobijoba_detail_parser_rejects_jobleads_redirect(jobijoba_redirect_page: tuple[str, str, str]) -> None:
    detail_url, redirect_url, detail_html = jobijoba_redirect_page
    target_url = "https://fr.jobleads.com/register/example"

    async def run_test() -> JobOffer | None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == redirect_url:
                return httpx.Response(302, headers={"location": target_url}, request=request)
            if str(request.url) == target_url:
                return httpx.Response(
                    200,
                    text="<html><body>Registration required</body></html>",
                    headers={"content-type": "text/html"},
                    request=request,
                )
            return httpx.Response(
                200,
                text=detail_html,
                headers={"content-type": "text/html"},
                request=request,
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(
                client,
                detail_url,
                "jobijoba",
                "communication",
                SearchConfig(),
            )

    assert asyncio.run(run_test()) is None


@pytest.mark.asyncio
async def test_jobijoba_detail_parser_rejects_jobleads_provider_metadata(
    mock_html_client: Callable[[str], httpx.AsyncClient],
) -> None:
    detail_url = "https://www.jobijoba.com/fr/annonce/54/c7e225bc09a4711321f713f9c9bdda07"
    detail_html = """
    <html><body>
      <div class="offer current">
        <h1 class="permalink-title">Chargé de Communication &amp; Marketing Digital</h1>
        <span>Toulouse</span><span>CDI</span><span>Jobmania</span>
      </div>
      <script>
        const tracking = {
          'detail': {"products":[{
            "dimension24": "jobleads.fr_3_4",
            "customerName": "JobLeads GmbH"
          }]}
        };
      </script>
    </body></html>
    """
    async with mock_html_client(detail_html) as client:
        offer = await aggregators._extract_detail_offer(client, detail_url, "jobijoba", "communication", SearchConfig())

    assert offer is None


@pytest.mark.asyncio
async def test_jobijoba_detail_parser_ignores_jobleads_in_similar_offers(
    mock_html_client: Callable[[str], httpx.AsyncClient],
) -> None:
    detail_url = "https://www.jobijoba.com/fr/annonce/54/example"
    detail_html = """
    <html><body>
      <div class="offer current">
        <h1 class="permalink-title">Chargé de communication H/F</h1>
        <span>Toulouse</span><span>CDI</span><span>Ville de Toulouse</span>
      </div>
      <script>
        const tracking = {
          'detail': {"products":[{"customerName":"Pole Emploi"}]},
          'impressions': [{"customerName":"JobLeads GmbH"}]
        };
      </script>
    </body></html>
    """
    async with mock_html_client(detail_html) as client:
        offer = await aggregators._extract_detail_offer(client, detail_url, "jobijoba", "communication", SearchConfig())

    assert offer is not None
