from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest

from scrapper.config import DirectAggregatorConfig, SearchConfig
from scrapper.config.settings import LocationScopeConfig
from scrapper.models import JobOffer
from scrapper.scrapers import aggregators, cadremploi, france_travail
from scrapper.storage import JobStore
from scrapper.text import url_key


def test_france_travail_reads_public_component_data() -> None:
    data = {
        "intituleOffre": "Chargé de communication",
        "descriptif": "Poste en CDI à Toulouse.",
        "lieuTravail": {"libelleLieuTravail": "Toulouse", "syntheseLieuTravail": "31 - TOULOUSE"},
        "syntheseContrat": "CDI - Contrat travail",
        "dateDerniereModification": "16 octobre 2026",
        "salaire": "Mensuel de 2650 Euros",
        "etablissement": {"libelleEnseigne": "Example", "urlEntreprise": "https://example.org"},
    }

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api-descriptifoffre/offre/215HLNT"
        return httpx.Response(200, json=data)

    async def run() -> JobOffer | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._extract_detail_offer(
                client,
                "https://candidat.francetravail.fr/offres/recherche/detail/215HLNT",
                "france_travail",
                "communication",
                SearchConfig(),
            )

    offer = asyncio.run(run())
    assert offer is not None
    assert offer.title == "Chargé de communication"
    assert offer.company == "Example"
    assert offer.location == "Toulouse"
    assert offer.employment_type == "CDI - Contrat travail"
    assert offer.salary == "Mensuel de 2650 Euros"
    assert offer.date_posted is None
    assert offer.source_id == "215HLNT"
    assert offer.raw == {"public_offer": data}


@pytest.mark.parametrize("status", [404, 410, 503])
def test_france_travail_expired_or_failed_offer(status: int) -> None:
    async def run() -> JobOffer | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(status))) as client:
            return await france_travail.extract_public_offer(
                client,
                "https://candidat.francetravail.fr/offres/recherche/detail/215HLNT",
                "communication",
                SearchConfig(),
            )

    if status == 503:
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(run())
    else:
        assert asyncio.run(run()) is None


def test_isarta_uses_canonical_detail_and_preserves_original_link(toulouse_scope: LocationScopeConfig) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/cgi-bin/emplois/pm.cgi"
        assert request.url.params["action"] == "display"
        assert request.url.params["login"] == "76715"
        job = {
            "@type": "JobPosting",
            "title": "Chargé marketing communication",
            "hiringOrganization": {"name": "Example"},
            "employmentType": "FULL_TIME",
            "description": "Gestion de la communication.",
            "jobLocation": {"address": {"addressRegion": "Occitanie"}},
        }
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text=(
                '<h1>Employer navigation</h1><h3 class="Lieu">Blagnac (31 - Haute-Garonne)</h3>'
                "<div>Publiée : 12/11/2026</div><div>Type d'emploi : CDI Temps plein</div>"
                '<a href="/candidats/placement.php">Placement candidat</a>'
                '<a href="/cgi-bin/emplois/externe_candidature.cgi?login=76715">Postuler chez le recruteur</a>'
                '<script type="application/ld+json">' + json.dumps(job) + "</script>"
            ),
        )

    config = SearchConfig(location_scope=toulouse_scope, include_terms=("communication",))

    async def run() -> JobOffer | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._extract_detail_offer(
                client, "https://isarta.fr/?job=76715", "isarta", "communication", config
            )

    offer = asyncio.run(run())
    assert offer is not None
    assert offer.source_url == "https://isarta.fr/?job=76715"
    assert offer.title == "Chargé marketing communication"
    assert offer.company == "Example"
    assert offer.description == "Gestion de la communication."
    assert offer.location == "Blagnac (31 - Haute-Garonne)"
    assert offer.date_posted == "12/11/2026"
    assert offer.employment_type == "CDI"
    assert offer.direct_url == "https://isarta.fr/cgi-bin/emplois/externe_candidature.cgi?login=76715"
    assert aggregators._offer_is_usable(offer, config)


def test_emploi_collectivites_uses_visible_duties_and_rejects_unavailable_offers() -> None:
    url = "https://www.emploi-collectivites.fr/offre-emploi-territoriale-toulouse/communication-123"
    structured_data = {
        "@type": "JobPosting",
        "title": "Chargé de communication",
        "description": "Accueil administratif",
        "jobLocation": {"address": {"addressLocality": "Different town"}},
    }
    opening = '<script type="application/ld+json">' + json.dumps(structured_data) + "</script>"
    duties = """
        <div class="section_offre"><div class="row"><b>Ce poste est à pourvoir à Toulouse .</b></div></div>
        <div id="texte_offre">Accueil administratif. Contexte et environnement du poste. Piloter les campagnes.</div>
    """

    async def run(html: str) -> JobOffer | None:
        transport = httpx.MockTransport(lambda _: httpx.Response(200, text=html, headers={"content-type": "text/html"}))
        config = SearchConfig()
        async with httpx.AsyncClient(transport=transport) as client:
            return await aggregators._extract_detail_offer(client, url, "emploi_collectivites", "communication", config)

    assert asyncio.run(run(opening)) is None
    offer = asyncio.run(run(opening + duties))
    assert offer is not None
    assert offer.title == "Chargé de communication"
    assert offer.location == "Toulouse"
    assert offer.description == "Contexte et environnement du poste. Piloter les campagnes."


def test_cadremploi_fallback_is_only_used_for_403(monkeypatch: pytest.MonkeyPatch) -> None:
    fallback = Mock(return_value=httpx.Response(200, text="public listing"))
    monkeypatch.setattr(cadremploi, "_tls_get", fallback)

    async def run(status: int) -> httpx.Response:
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


def test_cadremploi_transport_closes_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Mock(headers={})
    session.get.return_value = Mock(
        status_code=200, text="jobs", headers={"Content-Type": "text/html"}, url="https://www.cadremploi.fr/emploi"
    )
    monkeypatch.setattr(cadremploi, "TLSRotating", lambda: session)
    response = cadremploi._tls_get("https://www.cadremploi.fr/emploi", "test-agent")
    assert response.text == "jobs"
    assert response.headers["content-type"] == "text/html"
    session.get.assert_called_once_with("https://www.cadremploi.fr/emploi", timeout=20, allow_redirects=True)
    session.close.assert_called_once()


def test_cadremploi_query_ids_do_not_collide() -> None:
    assert url_key("https://www.cadremploi.fr/emploi/detail_offre?offreId=123&utm_source=x") == (
        "www.cadremploi.fr/emploi/detail_offre?offreId=123"
    )
    assert url_key("https://www.cadremploi.fr/emploi/detail_offre?offreId=123") != url_key(
        "https://www.cadremploi.fr/emploi/detail_offre?offreId=456"
    )


@pytest.mark.parametrize("use_modified_date", [False, True])
def test_direct_relevance_order_keeps_recent_offer_after_old_offer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, use_modified_date: bool
) -> None:
    async def links(*args: object, **kwargs: object) -> list[str]:
        return ["https://example.org/jobs/old", "https://example.org/jobs/new"]

    async def extract(
        client: httpx.AsyncClient, url: str, source: str, query: str, config: SearchConfig, **kwargs: object
    ) -> JobOffer:
        date = "2000-01-01" if url.endswith("old") else datetime.now(UTC).date().isoformat()
        return JobOffer(
            source=source,
            source_url=url,
            title="Example role",
            company="Example",
            location="Toulouse",
            date_posted=None if use_modified_date else date,
            raw={"public_offer": {"dateDerniereModification": date}} if use_modified_date else None,
        )

    monkeypatch.setattr(aggregators, "_detail_links", links)
    monkeypatch.setattr(aggregators, "_extract_detail_offer", extract)
    config = SearchConfig(
        queries=("example role",), location_scope={"enabled": False}, aggregators={"direct_locations": ["76R"]}
    )
    store = JobStore(tmp_path / "jobs.duckdb")

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await aggregators._scrape_direct_spec(client, config.aggregators.direct_specs[0], config, store)

    try:
        assert asyncio.run(run()) == 1
        assert store.raw_dataframe().iloc[0]["source_url"].endswith("/new")
    finally:
        store.close()


def test_direct_budget_allows_later_titles_and_regions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def links(
        client: httpx.AsyncClient, spec: DirectAggregatorConfig, search_url: str, *args: object, **kwargs: object
    ) -> list[str]:
        return [f"{search_url}/1", f"{search_url}/2"]

    async def extract(
        client: httpx.AsyncClient, url: str, source: str, query: str, config: SearchConfig, **kwargs: object
    ) -> JobOffer:
        return JobOffer(
            source=source, source_url=url, title="Example role", company="Example", location="Paris", search_query=query
        )

    monkeypatch.setattr(aggregators, "_detail_links", links)
    monkeypatch.setattr(aggregators, "_extract_detail_offer", extract)
    config = SearchConfig(
        queries=("example role", "sample role"), aggregators={"max_detail_pages": 1}, location_scope={"enabled": False}
    )
    spec = config.aggregators.direct_specs[0].model_copy(
        update={
            "search_url_templates": ("https://example.test/paris/{query}", "https://example.test/toulouse/{query}"),
            "use_direct_locations": False,
        }
    )
    store = JobStore(tmp_path / "jobs.duckdb")

    async def run() -> int:
        async with httpx.AsyncClient() as client:
            return await aggregators._scrape_direct_spec(client, spec, config, store)

    try:
        assert asyncio.run(run()) == 4
        assert set(store.raw_dataframe()["search_query"]) == {"example role", "sample role"}
    finally:
        store.close()


def test_makesense_prioritizes_matching_local_cards_before_consuming_detail_budget(
    toulouse_scope: LocationScopeConfig,
) -> None:
    config = SearchConfig(location_scope=toulouse_scope, include_terms=("communication",))
    spec = next(s for s in config.aggregators.direct_specs if s.source == "makesense").model_copy(
        update={"max_links": 1}
    )
    html = """
        <a href="/fr/jobs/stage-123">Stage communication</a>
        <a href="/fr/jobs/alternance-456">Chargé communication en alternance</a>
        <a href="/fr/jobs/marketing-agency-555">
            <h3 class="job__title">Technicien support</h3>Communication SA <address>Toulouse, France</address>
        </a>
        <a href="/fr/jobs/paris-567">
            <h3 class="job__title">Chargé de communication</h3><address>Paris, France</address>
        </a>
        <a href="/fr/jobs/cdi-789">
            <h3 class="job__title">Chargé de communication</h3><address>Toulouse, France</address>
        </a>
    """

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, text=html))) as client:
            return await aggregators._detail_links(
                client, spec, "https://jobs.makesense.org/fr/s/jobs/all", config=config
            )

    assert asyncio.run(run()) == ["https://jobs.makesense.org/fr/jobs/cdi-789"]


def test_optioncarriere_verification_page_is_a_failure_not_empty_search() -> None:
    async def run() -> list[str]:
        transport = httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                text='<h1>Vérification requise</h1><script src="https://captcha.careerjet.net/v1/api.js"></script>',
                headers={"content-type": "text/html"},
            )
        )
        async with httpx.AsyncClient(transport=transport) as client:
            return await aggregators._direct_site_search_results(
                client,
                "optioncarriere",
                "communication",
                SearchConfig(locations=("Toulouse, France",)),
            )

    with pytest.raises(RuntimeError, match="verification challenge"):
        asyncio.run(run())


def test_isarta_honors_configured_regional_discovery_page(toulouse_scope: LocationScopeConfig) -> None:
    config = SearchConfig(
        location_scope=toulouse_scope, include_terms=("communication",), aggregators={"max_detail_pages": 1}
    )
    spec = next(s for s in config.aggregators.site_search_sources if s.source == "isarta").model_copy(
        update={
            "search_url_templates": ("https://isarta.fr/cgi-bin/emplois/jobs?cat=occitanie",),
        }
    )

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.params["cat"] == "occitanie"
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="""
            <a href="https://isarta.fr/?job=125">
                <h2 class="poste-listing-monopage">Responsable commercial</h2>
                <h3>Communication SA</h3><h4 class="lieu-listing-monopage">Toulouse</h4>
            </a>
            <a href="https://isarta.fr/?job=127">
                <h2 class="poste-listing-monopage">Chargé de communication</h2>
                <h4 class="lieu-listing-monopage">Saint-Étienne</h4>
            </a>
            <a href="https://isarta.fr/?job=123">
                <h2 class="poste-listing-monopage">Chargé de communication</h2>
                <h4 class="lieu-listing-monopage">Blagnac (31 - Haute-Garonne)</h4>
            </a>
        """,
        )

    async def run() -> list[str]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await aggregators._site_search_results(client, spec, "communication", config)

    assert asyncio.run(run()) == ["https://isarta.fr/?job=123"]
