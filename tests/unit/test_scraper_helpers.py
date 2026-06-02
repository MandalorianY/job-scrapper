from __future__ import annotations

from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.scrapers.aggregators import _company_from_domain as aggregator_company_from_domain
from scrapper.scrapers.aggregators import _has_regional_signal, _infer_contract, _offer_is_usable
from scrapper.scrapers.common import apply_url, json_ld_jobposting
from scrapper.scrapers.web_search import (
    _company_from_domain,
    _is_excluded_domain,
    _is_info_page,
    _looks_like_job,
    _looks_regional,
    _unwrap_ddg,
)


def test_json_ld_jobposting_finds_nested_graph_payload() -> None:
    soup = BeautifulSoup(
        """
        <html><head>
        <script type="application/ld+json">
        {"@graph":[{"@type":"Organization","name":"Example"},{"@type":"JobPosting","title":"Example coordinator"}]}
        </script>
        </head></html>
        """,
        "lxml",
    )

    assert json_ld_jobposting(soup) == {"@type": "JobPosting", "title": "Example coordinator"}


def test_apply_url_prefers_apply_link_and_ignores_share_links() -> None:
    soup = BeautifulSoup(
        """
        <html><body>
          <a href="https://www.linkedin.com/shareArticle?url=https://jobs.example/1">Partager</a>
          <a href="/jobs/1">Voir l'offre</a>
          <a class="btn btn-primary" href="https://ats.example/apply/1">Postuler maintenant</a>
        </body></html>
        """,
        "lxml",
    )

    assert apply_url(soup, "https://jobs.example/jobs/1") == "https://ats.example/apply/1"


def test_apply_url_ignores_generic_one_tap_form() -> None:
    soup = BeautifulSoup(
        """
        <html><body>
          <form id="formCustomOneTap" action="/fr-fr/candidat/onetapturbocustom"></form>
          <a href="#postuler">Postuler</a>
        </body></html>
        """,
        "lxml",
    )

    assert apply_url(soup, "https://www.hellowork.com/fr-fr/emplois/1.html") == (
        "https://www.hellowork.com/fr-fr/emplois/1.html#postuler"
    )


def test_web_search_url_and_page_classification_helpers() -> None:
    assert (
        _unwrap_ddg("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcompany.example%2Fjobs%2F1")
        == "https://company.example/jobs/1"
    )
    assert _is_excluded_domain("https://www.linkedin.com/jobs/view/1")
    assert _is_info_page("https://example.com/fiches-metier/example-role")
    assert _company_from_domain("https://jobs.my-company.example/offres/1") == "My Company"
    assert _looks_like_job("Example coordinator full-time", None)
    assert _looks_regional(None, "Role based in Grand Annecy")


def test_aggregator_offer_usability_requires_title_contract_and_region() -> None:
    config = SearchConfig()
    usable = type(
        "Offer",
        (),
        {
            "title": "Example coordinator",
            "employment_type": "CDI",
            "description": "Role based in Lyon.",
            "location": "Lyon",
        },
    )()
    bad_contract = type(
        "Offer",
        (),
        {
            "title": "Example coordinator",
            "employment_type": "Alternance",
            "description": "Role based in Lyon.",
            "location": "Lyon",
        },
    )()

    assert _offer_is_usable(usable, config)
    assert not _offer_is_usable(bad_contract, config)
    assert _infer_contract("CDI ou CDD possible") == "CDI, CDD"
    assert _has_regional_signal(None, "Hybrid role with presence in Grenoble")
    assert aggregator_company_from_domain("https://www.emploi-collectivites.fr/offres/1") == "Emploi Collectivites"
