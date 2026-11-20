from __future__ import annotations

import asyncio
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter

from scrapper.config import SearchConfig
from scrapper.export import export_jobs
from scrapper.scrapers import (
    scrape_aggregators,
    scrape_hellowork,
    scrape_jobspy_sources,
    scrape_web_search,
    scrape_welcome_to_the_jungle,
)
from scrapper.storage import JobStore

RUN_REAL_SMOKE_ENV = "SCRAPPER_RUN_REAL_SMOKE"
SOURCE_FILTER_ENV = "SCRAPPER_REAL_SMOKE_SOURCE"


@dataclass(frozen=True)
class SmokeSource:
    source: str
    runner: Callable[[SearchConfig, JobStore], int | dict[str, int] | None]


def _jobspy_source(source: str) -> SmokeSource:
    def run(config: SearchConfig, store: JobStore) -> dict[str, int]:
        return scrape_jobspy_sources(config.model_copy(update={"jobspy_sites": (source,)}), store)

    return SmokeSource(source, run)


def _direct_aggregator_source(source: str) -> SmokeSource:
    def run(config: SearchConfig, store: JobStore) -> dict[str, int]:
        specs = tuple(spec for spec in config.aggregators.direct_specs if spec.source == source)
        assert specs, f"No direct aggregator config found for {source}"
        aggregators = config.aggregators.model_copy(update={"direct_specs": specs, "site_search_sources": ()})
        return asyncio.run(scrape_aggregators(config.model_copy(update={"aggregators": aggregators}), store))

    return SmokeSource(source, run)


def _site_search_source(source: str) -> SmokeSource:
    def run(config: SearchConfig, store: JobStore) -> dict[str, int]:
        specs = tuple(spec for spec in config.aggregators.site_search_sources if spec.source == source)
        assert specs, f"No site-search aggregator config found for {source}"
        aggregators = config.aggregators.model_copy(update={"direct_specs": (), "site_search_sources": specs})
        return asyncio.run(scrape_aggregators(config.model_copy(update={"aggregators": aggregators}), store))

    return SmokeSource(source, run)


_SMOKE_CONFIG = SearchConfig()
REAL_SMOKE_SOURCES = (
    *(_jobspy_source(source) for source in _SMOKE_CONFIG.jobspy_sites),
    SmokeSource("hellowork", lambda config, store: asyncio.run(scrape_hellowork(config, store))),
    SmokeSource("welcome_to_the_jungle", lambda config, store: asyncio.run(scrape_welcome_to_the_jungle(config, store))),
    SmokeSource("web_search", lambda config, store: asyncio.run(scrape_web_search(config, store))),
    *(_direct_aggregator_source(spec.source) for spec in _SMOKE_CONFIG.aggregators.direct_specs),
    *(_site_search_source(spec.source) for spec in _SMOKE_CONFIG.aggregators.site_search_sources),
)


@pytest.mark.integration
@pytest.mark.slow
@pytest.mark.parametrize("smoke_source", REAL_SMOKE_SOURCES, ids=[case.source for case in REAL_SMOKE_SOURCES])
def test_real_small_scrape_by_website(smoke_source: SmokeSource, tmp_path: Path) -> None:
    if os.getenv(RUN_REAL_SMOKE_ENV) != "1":
        pytest.skip(f"Set {RUN_REAL_SMOKE_ENV}=1 to run real website-by-website scraping smoke tests.")
    selected_source = os.getenv(SOURCE_FILTER_ENV)
    if selected_source and selected_source != smoke_source.source:
        pytest.skip(f"Set {SOURCE_FILTER_ENV}={smoke_source.source} to run this website smoke test.")
    if not _SMOKE_CONFIG.is_source_enabled(smoke_source.source):
        pytest.skip(f"{smoke_source.source} is disabled in the active configuration.")

    config = SearchConfig(
        queries=_SMOKE_CONFIG.queries[:1],
        results_wanted_per_query=1,
        custom_max_pages=1,
        custom_max_detail_pages=3,
        web_search_max_results_per_query=2,
        web_search_max_detail_pages=2,
        request_delay_secs=0.2,
        database_path=tmp_path / "job_search.duckdb",
        export_dir=tmp_path / "exports",
    )
    store = JobStore(config.database_path)
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Unverified HTTPS request.*")
            counts = smoke_source.runner(config, store)
        search_state = store.conn.execute(
            """
            SELECT source, last_seen_count, last_error
            FROM search_state
            ORDER BY source, query
            """
        ).fetchall()
        assert search_state, f"{smoke_source.source} did not record search state; counts={counts!r}"

        excel_path, csv_path, raw_csv_path, total = export_jobs(
            store,
            config.export_dir,
            config.export.excel_columns,
            repair_fields_enabled_by_default=config.export.repair_fields_enabled_by_default,
            repair_fields_by_source=config.export.repair_fields_by_source,
            direct_links_enabled_by_source=config.export.direct_links_enabled_by_source,
        )
        assert excel_path.exists()
        assert csv_path.exists()
        assert raw_csv_path.exists()

        workbook = load_workbook(excel_path, data_only=False)
        worksheet = workbook["Offres"]
        assert "Données brutes" in workbook.sheetnames
        assert [cell.value for cell in worksheet[1]] == list(config.export.excel_columns)
        if total:
            assert "TableOffres" in worksheet.tables
            expected_range = f"A1:{get_column_letter(worksheet.max_column)}{worksheet.max_row}"
            assert worksheet.tables["TableOffres"].ref == expected_range
            assert worksheet.tables["TableOffres"].autoFilter.ref == expected_range
            if "Statut" in config.export.excel_columns:
                assert worksheet.data_validations.dataValidation
                assert worksheet.conditional_formatting
            if "Postulé" in config.export.excel_columns and "Statut" in config.export.excel_columns:
                status_col = config.export.excel_columns.index("Statut") + 1
                status_cell = f"{get_column_letter(status_col)}2"
                applied_col = config.export.excel_columns.index("Postulé") + 1
                assert worksheet.cell(2, applied_col).value == f'={status_cell}="Postulé"'

        print(f"Live source={smoke_source.source} stored={counts!r} exported={total} states={search_state!r}")

        errors = [f"{source}: {error}" for source, _seen_count, error in search_state if error]
        if errors:
            pytest.xfail("; ".join(errors))
    finally:
        store.close()
