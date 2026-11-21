from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from scrapper.config import SearchConfig
from scrapper.models import JobOffer
from scrapper.scrapers.aggregators import detail, scrape
from scrapper.storage import JobStore


def test_blocked_detail_page_does_not_become_unverified_offer() -> None:
    async def run() -> JobOffer | None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403))) as client:
            return await detail._extract_detail_offer(
                client,
                "https://isarta.fr/?job=123",
                "isarta",
                "communication",
                SearchConfig(),
            )

    assert asyncio.run(run()) is None


@pytest.mark.asyncio
async def test_jobijoba_detail_parser_rejects_alternance_offer(
    jobijoba_alternance_page: tuple[str, str], mock_html_client: Callable[[str], httpx.AsyncClient]
) -> None:
    url, html = jobijoba_alternance_page
    config = SearchConfig()
    async with mock_html_client(html) as client:
        offer = await detail._extract_detail_offer(client, url, "jobijoba", "communication", config)

    assert offer is not None
    assert offer.location == "Castelginest"
    assert offer.employment_type == "Alternance"
    assert offer.date_posted == "Il y a 10 h"
    assert "alternant(e)" in (offer.description or "")
    assert not detail._offer_is_usable(offer, SearchConfig())


def test_generic_date_inference_keeps_only_the_date_phrase() -> None:
    assert detail._infer_date("Publié le 04 juin 2026 Descriptif de l'offre") == "04 juin 2026"
    assert detail._infer_date("Publiée le Il y a 10 h Description de l'offre") == "Il y a 10 h"


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

    assert not detail._offer_is_usable(offer, config)


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

    assert detail._offer_is_usable(offer, config)


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
        offer = await detail._extract_detail_offer(client, url, "meteojob", "communication", SearchConfig())

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
        offer = await detail._extract_detail_offer(client, url, "emploi_territorial", "communication", config)

    assert offer is not None
    assert offer.title == "Chargé de communication"
    assert offer.company == "MAIRIE DE TOULOUSE"
    assert offer.location == "Toulouse (Haute-Garonne (31))"
    assert offer.employment_type == "Emploi permanent - vacance d'emploi"
    assert detail._offer_is_usable(offer, config)


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
    detail_html = detail_html.replace(
        "</body>", f'<script type="application/ld+json">{json.dumps(job)}</script></body>'
    )
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
            return await detail._extract_detail_offer(
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


def test_jobijoba_external_target_matches_full_hostname() -> None:
    target_url = "https://careers-jobijoba.com.example/jobs/123"
    assert detail._jobijoba_external_target(target_url) == target_url
    assert detail._jobijoba_external_target("https://WWW.JOBIJOBA.COM.:443/fr/annonce/54/example") is None


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
    detail_html = detail_html.replace(
        "</body>", f'<script type="application/ld+json">{json.dumps(job)}</script></body>'
    )
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
            assert await scrape._scrape_direct_spec(client, spec, config, store) == 0
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
            return await detail._extract_detail_offer(
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
        offer = await detail._extract_detail_offer(client, detail_url, "jobijoba", "communication", SearchConfig())

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
        offer = await detail._extract_detail_offer(client, detail_url, "jobijoba", "communication", SearchConfig())

    assert offer is not None
