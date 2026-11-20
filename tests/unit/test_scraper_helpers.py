from __future__ import annotations

from datetime import UTC, datetime

from bs4 import BeautifulSoup

from scrapper.config import SearchConfig
from scrapper.dates import absolute_posted_date
from scrapper.scrapers.aggregators import _company_from_domain as aggregator_company_from_domain
from scrapper.scrapers.aggregators import _has_regional_signal, _infer_contract, _offer_is_usable
from scrapper.scrapers.common import (
    apply_url,
    custom_search_cutoff,
    json_ld_jobposting,
    parse_posted_date,
    posted_date_is_before_cutoff,
)
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


def test_apply_url_reads_data_redirect_buttons() -> None:
    soup = BeautifulSoup(
        """
        <html><body>
          <span class="button-primary" data-redirect="/fr/redirect/offer/54/abc">Postuler</span>
        </body></html>
        """,
        "lxml",
    )

    assert apply_url(soup, "https://www.jobijoba.com/fr/annonce/54/abc") == (
        "https://www.jobijoba.com/fr/redirect/offer/54/abc"
    )


def test_parse_posted_date_handles_provider_formats() -> None:
    now = datetime(2026, 6, 5, 12, 0, tzinfo=UTC)

    assert parse_posted_date("2026-06-04T10:30:00Z", now=now).day.isoformat() == "2026-06-04"
    assert parse_posted_date("Publié le 04/06/2026", now=now).day.isoformat() == "2026-06-04"
    assert parse_posted_date("Il y a 10 h", now=now).moment == datetime(2026, 6, 5, 2, 0, tzinfo=UTC)
    assert parse_posted_date("28 mai 2026", now=now).day.isoformat() == "2026-05-28"
    assert absolute_posted_date("Il y a 19 h", now=now) == "2026-06-04T17:00:00+00:00"


def test_posted_date_before_cutoff_respects_day_precision() -> None:
    cutoff = datetime(2026, 6, 4, 15, 0, tzinfo=UTC)

    assert not posted_date_is_before_cutoff("2026-06-04", cutoff)
    assert posted_date_is_before_cutoff("2026-06-03", cutoff)
    assert posted_date_is_before_cutoff("2026-06-04T14:00:00Z", cutoff)


def test_custom_search_cutoff_uses_last_success_with_overlap() -> None:
    class Store:
        def get_last_success(self, source: str, query: str, location: str):
            return datetime(2026, 6, 4, 12, 0, tzinfo=UTC)

    class Config:
        default_hours_old = 336
        overlap_hours = 24

    cutoff = custom_search_cutoff(
        Store(),
        "hellowork",
        "communication",
        "Toulouse, France",
        Config(),
        now=datetime(2026, 6, 5, 12, 0, tzinfo=UTC),
    )

    assert cutoff == datetime(2026, 6, 3, 12, 0, tzinfo=UTC)


def test_web_search_url_and_page_classification_helpers() -> None:
    assert (
        _unwrap_ddg("https://duckduckgo.com/l/?uddg=https%3A%2F%2Fcompany.example%2Fjobs%2F1")
        == "https://company.example/jobs/1"
    )
    assert _is_excluded_domain("https://www.linkedin.com/jobs/view/1")
    assert _is_info_page("https://example.com/fiches-metier/example-role")
    assert _company_from_domain("https://jobs.my-company.example/offres/1") == "My Company"
    assert _looks_like_job("Example coordinator full-time", None)
    assert _looks_regional(None, "Role based in Grand Toulouse")


def test_aggregator_offer_usability_requires_title_contract_and_region() -> None:
    config = SearchConfig()
    usable = type(
        "Offer",
        (),
        {
            "title": "Example coordinator",
            "employment_type": "CDI",
            "description": "Role based in Toulouse.",
            "location": "Toulouse",
        },
    )()
    bad_contract = type(
        "Offer",
        (),
        {
            "title": "Example coordinator",
            "employment_type": "Alternance",
            "description": "Role based in Toulouse.",
            "location": "Toulouse",
        },
    )()

    assert _offer_is_usable(usable, config)
    assert not _offer_is_usable(bad_contract, config)
    assert _infer_contract("CDI ou CDD possible") == "CDI, CDD"
    assert _has_regional_signal(None, "Hybrid role with presence in Blagnac")
    assert aggregator_company_from_domain("https://www.emploi-collectivites.fr/offres/1") == "Emploi Collectivites"
