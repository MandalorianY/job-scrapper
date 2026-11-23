from __future__ import annotations

from scrapper.scrapers.jobspy_sources.hellowork import page_metadata
from scrapper.scrapers.jobspy_sources.offers import row_to_offer


def test_hellowork_keeps_detail_contract_advertised_pay_and_hybrid_schedule() -> None:
    metadata = page_metadata("""
        <div>
          <div><h1>Chargé de communication</h1><ul><li>CDD</li></ul></div>
          <ul>
            <li>Télétravail partiel</li>
            <li><button data-cy="salary-tag-button"
              data-analytics-values-param='{"wording":"Salaire réel"}'>
              <span>2 600 - 3 000 € / mois</span>
            </button></li>
          </ul>
        </div>
        <aside><div data-cy="contractCard">CDI</div></aside>
        """)
    assert metadata["listing_type"] == "CDD"
    assert metadata["min_amount"] == 2600
    assert metadata["max_amount"] == 3000
    assert metadata["work_from_home_type"] == "Télétravail partiel"
    offer = row_to_offer(metadata, "hellowork", "communication", "Toulouse")
    assert offer.employment_type == "CDD"
    assert offer.is_remote is True


def test_hellowork_ignores_estimated_pay_and_unrelated_cards() -> None:
    metadata = page_metadata("""
        <div>
          <div><h1>Chargé de communication</h1></div>
          <button data-cy="salary-tag-button" data-analytics-values-param='{"wording":"Salaire estimé"}'>
            25 000 - 30 000 € / an
          </button>
        </div>
        <aside><div data-cy="contractCard">CDI</div></aside>
        """)
    assert metadata == {}
