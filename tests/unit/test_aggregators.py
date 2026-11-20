from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from scrapper.config import SearchConfig, SiteSearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers import aggregators
from scrapper.storage import JobStore


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
                client, "isarta", "communication", SearchConfig(include_terms=("communication",))
            )

    assert asyncio.run(run()) == ["https://isarta.fr/?job=123"]


def test_cadremploi_all_blocked_pages_raise_instead_of_empty_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url))
    )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            return await aggregators._direct_site_search_results(client, "cadremploi", "communication", SearchConfig())

    with pytest.raises(RuntimeError, match="cadremploi discovery failed: HTTP 403"):
        asyncio.run(run())


def test_cadremploi_continues_to_working_location_after_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(
        aggregators.SITE_SEARCH_DIRECT_TEMPLATES,
        "cadremploi",
        (
            "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=toulouse-31",
            "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=colomiers-31",
        ),
    )
    monkeypatch.setattr(
        aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url))
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("ville") != "colomiers-31":
            return httpx.Response(403)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=('<a href="/emploi/detail_offre?annonceId=123">Chargé de communication</a>'),
        )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._direct_site_search_results(
                client, "cadremploi", "communication", SearchConfig(include_terms=("communication",))
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
    config = SearchConfig(distance_km=10)
    specs = {spec.source: spec for spec in config.aggregators.direct_specs}

    france_travail = aggregators._search_urls(specs["france_travail"], "chargé communication", config)
    assert all("rayon=10" in url for url in france_travail)
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
        enabled_sources={"apec": False},
        database_path=tmp_path / "jobs.duckdb",
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


def test_jobijoba_detail_parser_resolves_redirect_target(jobijoba_redirect_page: tuple[str, str, str]) -> None:
    detail_url, redirect_url, detail_html = jobijoba_redirect_page
    target_url = "https://www.hellowork.com/fr-fr/emplois/79739983.html?utm_source=jobijoba"
    redirect_html = f"""
    <html><body>
      <a href="https://r.holeest.com/redirect?from={target_url}">Continuer</a>
    </body></html>
    """

    async def run_test() -> JobOffer | None:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == redirect_url:
                return httpx.Response(200, text=redirect_html, headers={"content-type": "text/html"}, request=request)
            return httpx.Response(200, text=detail_html, headers={"content-type": "text/html"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(
                client, detail_url, "jobijoba", "communication", SearchConfig()
            )

    offer = asyncio.run(run_test())

    assert offer is not None
    assert offer.direct_url == target_url
    assert offer.source_id == "www.hellowork.com/fr-fr/emplois/79739983.html"


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
