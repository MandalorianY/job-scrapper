from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers import aggregators
from scrapper.storage import JobStore


def test_isarta_discovers_absolute_job_query_links() -> None:
    html = '''
        <a href="https://isarta.fr/?job=123"><img src="logo.png"></a>
        <a href="https://isarta.fr/?job=123">Chargé de communication Toulouse</a>
        <a href="https://isarta.fr/?job=123">Publié le 16 octobre</a>
        <a href="https://isarta.fr/?job=456">Développeur</a>
    '''

    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
        async with httpx.AsyncClient(transport=transport) as client:
            return await aggregators._direct_site_search_results(
                client, "isarta", "communication", SearchConfig(include_terms=("communication",))
            )

    assert asyncio.run(run()) == [aggregators.SearchResult(
        "https://isarta.fr/?job=123", "Chargé de communication Toulouse", None
    )]


def test_cadremploi_all_blocked_pages_raise_instead_of_empty_success(monkeypatch) -> None:
    monkeypatch.setattr(aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            return await aggregators._direct_site_search_results(client, "cadremploi", "communication", SearchConfig())

    with pytest.raises(RuntimeError, match="cadremploi discovery failed: HTTP 403"):
        asyncio.run(run())


def test_cadremploi_continues_to_working_location_after_http_error(monkeypatch) -> None:
    monkeypatch.setitem(
        aggregators.SITE_SEARCH_DIRECT_TEMPLATES,
        "cadremploi",
        (
            "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=toulouse-31",
            "https://www.cadremploi.fr/emploi/liste_offres?motscles={query}&ville=colomiers-31",
        ),
    )
    monkeypatch.setattr(aggregators.cadremploi, "_tls_get", lambda url, _: httpx.Response(403, request=httpx.Request("GET", url)))
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.params.get("ville") != "colomiers-31":
            return httpx.Response(403)
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<a href="/emploi/detail_offre?annonceId=123">Chargé de communication</a>'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._direct_site_search_results(
                client, "cadremploi", "communication", SearchConfig(include_terms=("communication",))
            )

    assert [result.url for result in asyncio.run(run())] == [
        "https://www.cadremploi.fr/emploi/detail_offre?annonceId=123"
    ]


def test_makesense_navigation_does_not_consume_detail_limit() -> None:
    config = SearchConfig()
    spec = next(s for s in config.aggregators.direct_specs if s.source == "makesense").model_copy(update={"max_links": 1})
    html = '''
        <a href="/fr/s/jobs/all/all">All jobs</a>
        <a href="/fr/s/jobs/all/all/stage">Internships</a>
        <a href="/fr/jobs/employer-communication-123">Chargé de communication</a>
    '''

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
            return await aggregators._detail_links(client, spec, "https://jobs.makesense.org/fr/s/jobs/all/all")

    assert asyncio.run(run()) == ["https://jobs.makesense.org/fr/jobs/employer-communication-123"]


def test_blocked_title_only_result_does_not_become_unverified_offer() -> None:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            return await aggregators._extract_result_offer(
                client, aggregators.SearchResult("https://isarta.fr/?job=123", "Communication Toulouse", None),
                "isarta", "communication", "Toulouse", SearchConfig(),
            )

    assert asyncio.run(run()) is None


def test_disabled_site_search_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    async def fake_site_search_results(
        client: httpx.AsyncClient,
        spec,
        query: str,
        search_query: str,
        config: SearchConfig,
    ) -> list[aggregators.SearchResult]:
        return [aggregators.SearchResult(url=f"https://{spec.host}/jobs/example", title="Example role", snippet=None)]

    async def fake_extract_result_offer(
        client: httpx.AsyncClient,
        result: aggregators.SearchResult,
        source: str,
        query: str,
        search_location: str,
        config: SearchConfig,
    ):
        return None

    monkeypatch.setattr(aggregators, "_site_search_results", fake_site_search_results)
    monkeypatch.setattr(aggregators, "_extract_result_offer", fake_extract_result_offer)

    config = SearchConfig(
        queries=("example role",),
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
    finally:
        store.close()


def test_jobijoba_detail_parser_rejects_alternance_offer() -> None:
    url = "https://www.jobijoba.com/fr/annonce/54/66cdbc5f83c4667724e965945b83f122"
    html = """
    <html>
      <head>
        <meta name="description" content="Voir l'offre d'emploi - Jobijoba">
      </head>
      <body>
        <nav>
          <span>Emploi CDI/CDD</span>
          <span>Offres d'alternance</span>
        </nav>
        <div class="offer current">
          <div class="row permalink-open">
            <h1 class="permalink-title">Chargé de communication et marketing digital h/f</h1>
            <span>Castelginest</span>
            <span>Alternance</span>
            <span>Chargé de communication</span>
            <span>De 780 € à 1 222 € par mois</span>
          </div>
          <span>Publiée le Il y a 10 h</span>
          <h2>Description de l'offre</h2>
          <p class="permalink-description">
            Dans le cadre de son partenariat avec une entreprise spécialisée dans l'immobilier,
            notre école recherche un(e) alternant(e) pour occuper un poste.
          </p>
        </div>
      </body>
    </html>
    """

    async def run_test():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html; charset=UTF-8"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(client, url, "jobijoba", "communication", SearchConfig())

    offer = asyncio.run(run_test())

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
    config = SearchConfig(include_terms=("communication",))
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

    assert aggregators._is_meteojob_placeholder_offer(offer)
    assert not aggregators._offer_is_usable(offer, config)


def test_specific_undated_meteojob_offer_is_not_rejected_as_placeholder() -> None:
    config = SearchConfig(include_terms=("communication",))
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

    assert not aggregators._is_meteojob_placeholder_offer(offer)
    assert aggregators._offer_is_usable(offer, config)


def test_detail_parser_extracts_direct_apply_url() -> None:
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

    async def run_test():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html; charset=UTF-8"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(client, url, "meteojob", "communication", SearchConfig())

    offer = asyncio.run(run_test())

    assert offer is not None
    assert offer.direct_url == "https://ats.example/apply/example"


def test_emploi_territorial_detail_parser_uses_source_specific_title() -> None:
    url = "https://www.emploi-territorial.fr/offre/o069-example-charge-communication"
    config = SearchConfig(
        include_terms=("communication",),
        exclude_terms=("stage", "alternance", "apprentissage"),
        location_scope={"enabled": False},
    )
    html = """
    <html>
      <head>
        <title>Chargé de communication - MAIRIE DE TOULOUSE | emploi-territorial.fr</title>
        <meta property="og:title" content="www.emploi-territorial.fr">
      </head>
      <body>
        <main>
          <h1 class="set-header-logo-h1"></h1>
          <h2 class="set-line-emploi">Chargé de communication</h2>
          <div class="offre-item row p-2">
            <div class="offre-item-label col-4 font-weight-bold">Employeur</div>
            <div class="offre-item-value col-8"><strong>MAIRIE DE TOULOUSE</strong></div>
          </div>
          <div class="offre-item row p-2">
            <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Lieu de travail</div>
            <div class="offre-item-value col-12 col-sm-8">Toulouse (Haute-Garonne (31))</div>
          </div>
          <div class="offre-item row p-2">
            <div class="offre-item-label col-12 col-sm-4 font-weight-bold">Type d'emploi</div>
            <div class="offre-item-value col-12 col-sm-8">Emploi permanent - vacance d'emploi</div>
          </div>
          <div class="offre-item row p-2 font-weight-bold">
            <div class="offre-item-label col">Descriptif de l'emploi</div>
          </div>
          <div class="offre-item-text row px-3 mb-3">
            Poste de communication basé à Toulouse.
          </div>
        </main>
      </body>
    </html>
    """

    async def run_test():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html; charset=UTF-8"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(client, url, "emploi_territorial", "communication", config)

    offer = asyncio.run(run_test())

    assert offer is not None
    assert offer.title == "Chargé de communication"
    assert offer.company == "MAIRIE DE TOULOUSE"
    assert offer.location == "Toulouse (Haute-Garonne (31))"
    assert offer.employment_type == "Emploi permanent - vacance d'emploi"
    assert aggregators._offer_is_usable(offer, config)


def test_jobijoba_detail_parser_resolves_redirect_target() -> None:
    detail_url = "https://www.jobijoba.com/fr/annonce/54/example"
    redirect_url = "https://www.jobijoba.com/fr/redirect/offer/54/example"
    target_url = "https://www.hellowork.com/fr-fr/emplois/79739983.html?utm_source=jobijoba"
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
    redirect_html = f"""
    <html><body>
      <a href="https://r.holeest.com/redirect?from={target_url}">Continuer</a>
    </body></html>
    """

    async def run_test():
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == redirect_url:
                return httpx.Response(200, text=redirect_html, headers={"content-type": "text/html"}, request=request)
            return httpx.Response(200, text=detail_html, headers={"content-type": "text/html"}, request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(client, detail_url, "jobijoba", "communication", SearchConfig())

    offer = asyncio.run(run_test())

    assert offer is not None
    assert offer.direct_url == target_url
    assert offer.source_id == "www.hellowork.com/fr-fr/emplois/79739983.html"


def test_jobijoba_detail_parser_rejects_jobleads_redirect() -> None:
    detail_url = "https://www.jobijoba.com/fr/annonce/54/example"
    redirect_url = "https://www.jobijoba.com/fr/redirect/offer/54/example"
    target_url = "https://fr.jobleads.com/register/example"
    detail_html = """
    <html><body>
      <div class="offer current">
        <div class="row permalink-open">
          <h1 class="permalink-title">Chargé de communication H/F</h1>
          <span>Toulouse</span><span>CDI</span><span>Example</span>
        </div>
        <p class="permalink-description">Poste de communication basé à Toulouse.</p>
        <span class="button-primary" data-redirect="/fr/redirect/offer/54/example">Postuler</span>
      </div>
    </body></html>
    """

    async def run_test():
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


def test_jobijoba_detail_parser_rejects_jobleads_provider_metadata() -> None:
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

    async def run_test():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=detail_html,
                headers={"content-type": "text/html"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(
                client,
                detail_url,
                "jobijoba",
                "communication",
                SearchConfig(),
            )

    assert asyncio.run(run_test()) is None


def test_jobijoba_detail_parser_ignores_jobleads_in_similar_offers() -> None:
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

    async def run_test():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text=detail_html,
                headers={"content-type": "text/html"},
                request=request,
            )
        )
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            return await aggregators._extract_detail_offer(
                client,
                detail_url,
                "jobijoba",
                "communication",
                SearchConfig(),
            )

    assert asyncio.run(run_test()) is not None
