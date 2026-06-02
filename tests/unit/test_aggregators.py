from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

import scrapper.scrapers.aggregators as aggregators
from scrapper.config import SearchConfig
from scrapper.storage import JobStore


def test_disabled_site_search_source_is_skipped(monkeypatch, tmp_path: Path) -> None:
    async def fake_site_search_results(
        client: httpx.AsyncClient,  # noqa: ARG001
        spec,
        query: str,  # noqa: ARG001
        search_query: str,  # noqa: ARG001
        config: SearchConfig,  # noqa: ARG001
    ) -> list[aggregators.SearchResult]:
        return [aggregators.SearchResult(url=f"https://{spec.host}/jobs/example", title="Example role", snippet=None)]

    async def fake_extract_result_offer(
        client: httpx.AsyncClient,  # noqa: ARG001
        result: aggregators.SearchResult,  # noqa: ARG001
        source: str,
        query: str,  # noqa: ARG001
        search_location: str,
        config: SearchConfig,  # noqa: ARG001
    ):
        return None

    monkeypatch.setattr(aggregators, "_site_search_results", fake_site_search_results)
    monkeypatch.setattr(aggregators, "_extract_result_offer", fake_extract_result_offer)

    config = SearchConfig(
        queries=("example role",),
        locations=("Lyon, France",),
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
            <span>Annemasse</span>
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
    assert offer.location == "Annemasse"
    assert offer.employment_type == "Alternance"
    assert offer.date_posted == "Il y a 10 h"
    assert "alternant(e)" in (offer.description or "")
    assert not aggregators._offer_is_usable(offer, SearchConfig())


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
          "jobLocation": {"address": {"addressLocality": "Lyon"}},
          "description": "Poste de communication basé à Lyon.",
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
