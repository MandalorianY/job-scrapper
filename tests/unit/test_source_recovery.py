from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import Mock

import httpx
import pytest

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers import aggregators, cadremploi, france_travail
from scrapper.storage import JobStore
from scrapper.text import url_key


def test_france_travail_reads_public_component_data() -> None:
    data = {
        "intituleOffre": "Chargé de communication", "descriptif": "Poste en CDI à Toulouse.",
        "lieuTravail": "TOULOUSE (31)", "syntheseContrat": "CDI - Contrat travail",
        "dateDerniereModification": "16 octobre 2026", "salaire": "Mensuel de 2650 Euros",
        "etablissement": {"libelleEnseigne": "Example", "urlEntreprise": "https://example.org"},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api-descriptifoffre/offre/215HLNT"
        return httpx.Response(200, json=data)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._extract_detail_offer(
                client, "https://candidat.francetravail.fr/offres/recherche/detail/215HLNT",
                "france_travail", "communication", SearchConfig(),
            )

    offer = asyncio.run(run())
    assert offer.title == "Chargé de communication"
    assert offer.company == "Example"
    assert offer.location == "TOULOUSE (31)"
    assert offer.employment_type == "CDI - Contrat travail"
    assert offer.salary == "Mensuel de 2650 Euros"
    assert offer.date_posted == "16 octobre 2026"
    assert offer.source_id == "215HLNT"
    assert offer.raw == {"public_offer": data}


@pytest.mark.parametrize("status", [404, 410, 503])
def test_france_travail_expired_or_failed_offer(status: int) -> None:
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
            return await france_travail.extract_public_offer(
                client, "https://candidat.francetravail.fr/offres/recherche/detail/215HLNT",
                "communication", SearchConfig(),
            )

    if status == 503:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) is None


def test_isarta_uses_canonical_detail_and_preserves_original_link() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/emplois/pm.cgi"
        assert request.url.params["action"] == "display"
        assert request.url.params["login"] == "76715"
        job = {
            "@type": "JobPosting", "title": "Chargé marketing communication",
            "hiringOrganization": {"name": "Example"}, "employmentType": "FULL_TIME",
            "description": "Gestion de la communication à Toulouse.",
            "jobLocation": {"address": {"addressLocality": "Toulouse"}},
        }
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<h1>Employer navigation</h1><script type="application/ld+json">' + json.dumps(job) + '</script>'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._extract_detail_offer(
                client, "https://isarta.fr/?job=76715", "isarta", "communication", SearchConfig()
            )

    offer = asyncio.run(run())
    assert offer.source_url == "https://isarta.fr/?job=76715"
    assert offer.title == "Chargé marketing communication"
    assert offer.company == "Example"
    assert offer.description == "Gestion de la communication à Toulouse."


def test_cadremploi_fallback_is_only_used_for_403(monkeypatch) -> None:
    fallback = Mock(return_value=httpx.Response(200, text="public listing"))
    monkeypatch.setattr(cadremploi, "_tls_get", fallback)

    async def run(status: int):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status)), headers={"user-agent": "test-agent"}
        ) as client:
            return await cadremploi.get_page(client, "https://www.cadremploi.fr/emploi/liste_offres")

    assert asyncio.run(run(200)).status_code == 200
    fallback.assert_not_called()
    assert asyncio.run(run(403)).text == "public listing"
    fallback.assert_called_once_with("https://www.cadremploi.fr/emploi/liste_offres", "test-agent")
    assert asyncio.run(run(429)).status_code == 429
    assert fallback.call_count == 1


def test_cadremploi_transport_closes_session(monkeypatch) -> None:
    session = Mock(headers={})
    session.get.return_value = Mock(status_code=200, text="jobs", headers={"Content-Type": "text/html"}, url="https://www.cadremploi.fr/emploi")
    monkeypatch.setattr(cadremploi, "create_session", lambda: session)
    response = cadremploi._tls_get("https://www.cadremploi.fr/emploi", "test-agent")
    assert response.text == "jobs"
    assert response.headers["content-type"] == "text/html"
    session.close.assert_called_once()


def test_cadremploi_query_ids_do_not_collide() -> None:
    assert url_key("https://www.cadremploi.fr/emploi/detail_offre?offreId=123&utm_source=x") == (
        "www.cadremploi.fr/emploi/detail_offre?offreId=123"
    )
    assert url_key("https://www.cadremploi.fr/emploi/detail_offre?offreId=123") != url_key(
        "https://www.cadremploi.fr/emploi/detail_offre?offreId=456"
    )


def test_direct_relevance_order_keeps_recent_offer_after_old_offer(monkeypatch, tmp_path) -> None:
    async def links(*args, **kwargs):
        return ["https://example.org/jobs/old", "https://example.org/jobs/new"]

    async def extract(client, url, source, query, config, **kwargs):
        return JobOffer(
            source=source, source_url=url, title="Example role", company="Example", location="Toulouse",
            date_posted="2000-01-01" if url.endswith("old") else datetime.now(UTC).date().isoformat(),
        )

    monkeypatch.setattr(aggregators, "_detail_links", links)
    monkeypatch.setattr(aggregators, "_extract_detail_offer", extract)
    config = SearchConfig(queries=("example role",), location_scope={"enabled": False})
    store = JobStore(tmp_path / "jobs.duckdb")

    async def run():
        async with httpx.AsyncClient() as client:
            return await aggregators._scrape_direct_spec(client, config.aggregators.direct_specs[0], config, store)

    try:
        assert asyncio.run(run()) == 1
        assert store.raw_dataframe().iloc[0]["source_url"].endswith("/new")
    finally:
        store.close()


def test_direct_list_skips_student_contracts_before_consuming_detail_budget() -> None:
    config = SearchConfig(include_terms=("communication",))
    spec = next(s for s in config.aggregators.direct_specs if s.source == "makesense").model_copy(update={"max_links": 1})
    html = '''
        <a href="/fr/jobs/stage-123">Stage communication</a>
        <a href="/fr/jobs/alternance-456">Chargé communication en alternance</a>
        <a href="/fr/jobs/cdi-789">Chargé de communication CDI</a>
    '''

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
            return await aggregators._detail_links(client, spec, "https://jobs.makesense.org/fr/s/jobs/all", config=config)

    assert asyncio.run(run()) == ["https://jobs.makesense.org/fr/jobs/cdi-789"]


def test_optioncarriere_verification_page_is_a_failure_not_empty_search() -> None:
    async def run():
        transport = httpx.MockTransport(lambda _: httpx.Response(
            200, text='<h1>Vérification requise</h1><script src="https://captcha.careerjet.net/v1/api.js"></script>',
            headers={"content-type": "text/html"},
        ))
        async with httpx.AsyncClient(transport=transport) as client:
            return await aggregators._direct_site_search_results(client, "optioncarriere", "communication", SearchConfig())

    with pytest.raises(RuntimeError, match="verification challenge"):
        asyncio.run(run())


def test_isarta_honors_configured_regional_discovery_page() -> None:
    config = SearchConfig(include_terms=("communication",))
    spec = next(s for s in config.aggregators.site_search_sources if s.source == "isarta").model_copy(update={
        "search_url_templates": ("https://isarta.fr/cgi-bin/emplois/jobs?cat=auvergne-rhone-alpes",),
    })

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cat"] == "auvergne-rhone-alpes"
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            '<a href="https://isarta.fr/?job=123">Chargé de communication Toulouse</a>'
        ))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._site_search_results(client, spec, "communication", "unused search", config)

    assert [result.url for result in asyncio.run(run())] == ["https://isarta.fr/?job=123"]
