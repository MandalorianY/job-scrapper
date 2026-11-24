from __future__ import annotations

from pathlib import Path

import pytest

from jobsfr.offers.filters import ListingFilters
from jobsfr.offers.models import JobOffer
from jobsfr.storage import JobStore


def test_stores_cadremploi_rows_with_distinct_query_ids(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    url = "https://www.cadremploi.fr/emploi/detail_offre"
    try:
        for source_url, company in (
            (f"{url}?offreId=123", "First Employer"),
            (f"{url}?annonceId=456", "Second Employer"),
        ):
            store.upsert_offer(
                JobOffer(source="cadremploi", source_url=source_url, title="Communication", company=company)
            )
        assert store.conn.execute("SELECT DISTINCT source_id FROM raw_offers ORDER BY source_id").fetchall() == [
            ("123",),
            ("456",),
        ]
        assert set(store.canonical_dataframe()["company"]) == {"First Employer", "Second Employer"}
    finally:
        store.close()


def test_replay_preserves_contract_exclusions_from_original_database(tmp_path: Path) -> None:
    archive = JobStore(tmp_path / "archive.duckdb")
    try:
        for source, contract in (("indeed", "CDI"), ("linkedin", "Stage")):
            archive.upsert_offer(
                JobOffer(
                    source=source,
                    source_url=f"https://{source}.example/jobs/1",
                    title="Communication",
                    company="Same employer",
                    location="Toulouse",
                    employment_type=contract,
                )
            )
        assert archive.purge_excluded_offers() == 2
        original_exclusions = archive.excluded_url_keys()
        assert len(original_exclusions) == 2
        assert archive.replay_observations(archive.path) == 0
    finally:
        archive.close()

    rebuilt = JobStore(tmp_path / "rebuilt.duckdb")
    try:
        for _ in range(2):
            assert rebuilt.replay_observations(archive.path) == 0
            assert rebuilt.raw_dataframe().empty
            assert rebuilt.excluded_url_keys() == original_exclusions
    finally:
        rebuilt.close()


def test_replay_rolls_back_after_invalid_observation(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.duckdb"
    archive = JobStore(archive_path)
    try:
        archive.upsert_offer(JobOffer(source="board", source_url="https://example.test/jobs/1"))
        archive.conn.execute(
            "INSERT INTO offer_observations VALUES ('invalid', ?, now() + INTERVAL 1 SECOND, '{}')",
            [archive.run_id],
        )
    finally:
        archive.close()

    rebuilt = JobStore(tmp_path / "rebuilt.duckdb")
    try:
        with pytest.raises(KeyError, match="source"):
            rebuilt.replay_observations(archive_path)
        assert rebuilt.raw_dataframe().empty
    finally:
        rebuilt.close()


def test_contract_evidence_excludes_sparse_copies_of_same_offer(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        for source, description in [("indeed", None), ("glassdoor", "Lieu du stage : Toulouse")]:
            store.upsert_offer(
                JobOffer(
                    source=source,
                    source_url=f"https://{source}.example/jobs/1",
                    title="Responsable Marketing",
                    company="Same employer",
                    description=description,
                )
            )
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/2",
                source_id="permanent-2",
                title="Responsable Marketing",
                company="Other employer",
                employment_type="CDI",
            )
        )
        assert store.purge_excluded_offers() == 2
        assert store.raw_dataframe()["company"].tolist() == ["Other employer"]
        assert len(store.excluded_url_keys()) == 2
    finally:
        store.close()


def test_search_attempts_distinguish_empty_failed_and_unfinished(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.start_search("board", "analyst", "Toulouse")
        assert store.conn.execute("SELECT status FROM source_health").fetchone() == ("unfinished",)
        store.finish_search("board", "analyst", "Toulouse", 0)
        watermark = store.get_last_success("board", "analyst", "Toulouse")
        assert watermark is not None
        assert store.conn.execute("SELECT status, seen_count FROM source_health").fetchone() == ("succeeded", 0)
        store.start_search("board", "analyst", "Toulouse")
        store.finish_search("board", "analyst", "Toulouse", 0, error="Source unavailable")
        assert store.get_last_success("board", "analyst", "Toulouse") == watermark
        assert store.conn.execute("SELECT status, error FROM source_health").fetchone() == (
            "failed",
            "Source unavailable",
        )
        assert store.conn.execute(
            "SELECT count(*) FROM search_attempts WHERE run_id = ?", [store.run_id]
        ).fetchone() == (2,)
    finally:
        store.close()


def test_contract_purge_and_title_filter_preserve_nonmatching_raw_listings(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/stage",
                title="Stage example role",
                company="Example",
                employment_type="Stage",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/admin",
                title="Responsable administratif",
                company="Example",
                employment_type="CDI",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/com",
                title="Example coordinator",
                company="Example",
                employment_type="CDI",
            )
        )

        assert store.purge_excluded_offers() == 1
        assert len(store.raw_dataframe()) == 2
        filters = ListingFilters.model_validate({"title": {"include": ["example coordinator"]}})
        canonical = store.canonical_dataframe(allows=lambda row: filters.allows(row.get))
        assert canonical["title"].to_list() == ["Example coordinator"]
    finally:
        store.close()


def test_source_specific_company_filter_keeps_matching_duplicate(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb")
    try:
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://linkedin.example/jobs/france-travail",
                title="Example communications role",
                company="France Travail",
                location="Toulouse",
                description="Full-time communication role.",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/france-travail",
                title="Example communications role",
                company="France Travail",
                location="Toulouse",
                description="Full-time communication role.",
            )
        )

        filters = ListingFilters.model_validate(
            {"by_source": {"linkedin": {"company": {"exclude": {"names": ["France Travail"]}}}}}
        )
        canonical = store.canonical_dataframe(allows=lambda row: filters.allows(row.get))
        assert canonical["best_source"].to_list() == ["indeed"]
        assert canonical["sources"].to_list() == ["indeed"]
        assert len(store.raw_dataframe()) == 2
    finally:
        store.close()
