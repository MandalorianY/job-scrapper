from __future__ import annotations

from pathlib import Path
from typing import Mapping

from scrapper.config import DEFAULT_EXCLUDE_TERMS, DEFAULT_INCLUDE_TERMS, SearchConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore


def _store(
    tmp_path: Path,
    source_priority: Mapping[str, int] | None = None,
    default_source_priority: int = 100,
) -> JobStore:
    return JobStore(
        tmp_path / "jobs.duckdb",
        source_priority=source_priority,
        default_source_priority=default_source_priority,
    )


def test_storage_dedupes_by_title_company_and_keeps_most_complete_offer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/1?utm_source=test",
                direct_url="https://indeed.example/apply/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role.",
                employment_type="CDI",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="example_direct_board",
                source_url="https://jobs.example.com/detail/1",
                direct_url="https://jobs.example.com/detail/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role with a richer sample description.",
                employment_type="CDI",
                date_posted="2026-05-30",
                min_amount=36000,
                max_amount=44000,
                currency="EUR",
                interval="YEAR",
            )
        )

        canonical = store.canonical_dataframe()
        assert len(canonical) == 1
        row = canonical.iloc[0]
        assert row["best_source"] == "example_direct_board"
        assert row["duplicate_count"] == 2
        assert "indeed.example" in row["all_source_links"]
        assert "jobs.example.com" in row["all_source_links"]
    finally:
        store.close()


def test_storage_uses_configured_source_priority_for_primary_offer(tmp_path: Path) -> None:
    store = _store(
        tmp_path,
        source_priority={"indeed": 0, "linkedin": 10, "welcome_to_the_jungle": 20, "france_travail": 1000},
    )
    try:
        store.upsert_offer(
            JobOffer(
                source="welcome_to_the_jungle",
                source_url="https://welcometothejungle.example/jobs/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role with a richer sample description and detailed responsibilities.",
                employment_type="CDI",
                date_posted="2026-05-30",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role.",
                employment_type="CDI",
            )
        )

        canonical = store.canonical_dataframe()

        assert canonical.iloc[0]["best_source"] == "indeed"
        assert canonical.iloc[0]["best_source_url"] == "https://indeed.example/jobs/1"
        assert canonical.iloc[0]["date_posted"] == "2026-05-30"
    finally:
        store.close()


def test_storage_dedupes_by_substantial_matching_description(tmp_path: Path) -> None:
    store = _store(tmp_path)
    shared_description = (
        "You will lead campaign planning, coordinate publication calendars, partner with sales and product teams, "
        "track performance reporting, and maintain editorial governance across the organization."
    )
    try:
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/description-match-1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description=shared_description,
                employment_type="CDI",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://linkedin.example/jobs/description-match-2",
                title="Senior example communications partner",
                company="Example Organization",
                location="Example City",
                description=shared_description.upper(),
                employment_type="CDI",
                date_posted="2026-05-30",
            )
        )

        canonical = store.canonical_dataframe()

        assert len(canonical) == 1
        assert canonical.iloc[0]["duplicate_count"] == 2
        assert "indeed.example" in canonical.iloc[0]["all_source_links"]
        assert "linkedin.example" in canonical.iloc[0]["all_source_links"]
    finally:
        store.close()


def test_storage_ranks_configured_last_source_behind_unlisted_sources(tmp_path: Path) -> None:
    store = _store(tmp_path, source_priority={"france_travail": 1000}, default_source_priority=100)
    try:
        store.upsert_offer(
            JobOffer(
                source="france_travail",
                source_url="https://francetravail.example/jobs/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role with a richer sample description.",
                employment_type="CDI",
                date_posted="2026-05-30",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="meteojob",
                source_url="https://meteojob.example/jobs/1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
                description="Full-time example role.",
                employment_type="CDI",
            )
        )

        canonical = store.canonical_dataframe()

        assert canonical.iloc[0]["best_source"] == "meteojob"
    finally:
        store.close()


def test_storage_can_override_dedupe_fields(tmp_path: Path) -> None:
    store = JobStore(
        tmp_path / "jobs.duckdb",
        dedupe_primary_fields=("title",),
        dedupe_fallback_fields=(),
        dedupe_description_fields=(),
    )
    try:
        store.upsert_offer(
            JobOffer(
                source="indeed",
                source_url="https://indeed.example/jobs/custom-fields-1",
                title="Example coordinator",
                company="Example Organization",
                location="Example City",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://linkedin.example/jobs/custom-fields-2",
                title="Example coordinator",
                company="Another Organization",
                location="Another City",
            )
        )

        canonical = store.canonical_dataframe()

        assert len(canonical) == 1
        assert canonical.iloc[0]["duplicate_count"] == 2
    finally:
        store.close()


def test_default_config_declares_dedupe_source_priorities() -> None:
    config = SearchConfig()

    assert config.dedupe_fields.primary == ("title", "company")
    assert config.dedupe_fields.fallback == ("location",)
    assert config.dedupe_fields.description == ("description", "company", "location")
    assert config.dedupe_source_priority["indeed"] < config.dedupe_source_priority["linkedin"]
    assert config.dedupe_source_priority["linkedin"] < config.dedupe_source_priority["welcome_to_the_jungle"]
    assert config.dedupe_source_priority["france_travail"] > config.dedupe_default_source_priority
    assert config.export.excel_columns[:6] == (
        "Postulé",
        "Rejeté",
        "Lien",
        "Intitulé",
        "Entreprise",
        "Lieu",
    )
    assert config.export.repair_fields_enabled_by_default is True
    assert config.export.repair_fields_by_source["france_travail"] is True


def test_source_url_seen_uses_normalized_url_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="web_search",
                source_url="https://company.example/jobs/comms?utm_source=google",
                title="Example coordinator",
                company="Example",
            )
        )

        assert store.source_url_seen("web_search", "https://company.example/jobs/comms?utm_campaign=other")
    finally:
        store.close()


def test_purge_filters_remove_student_contracts_and_non_matching_titles(tmp_path: Path) -> None:
    store = _store(tmp_path)
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

        assert store.purge_excluded_offers(DEFAULT_EXCLUDE_TERMS) == 1
        assert store.purge_nonmatching_offers(DEFAULT_INCLUDE_TERMS) == 1
        canonical = store.canonical_dataframe()
        assert canonical["title"].to_list() == ["Example coordinator"]
    finally:
        store.close()


def test_purge_out_of_scope_offers_removes_locations_outside_configured_radius(tmp_path: Path) -> None:
    config = SearchConfig()
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/lyon",
                title="Example coordinator",
                company="Example",
                location="Pusignan, ARA, FR",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/thiers",
                title="Example coordinator",
                company="Example",
                location="Thiers, Auvergne-Rhône-Alpes, France",
            )
        )

        assert store.purge_out_of_scope_offers(lambda location: location_is_in_scope(location, config.location_scope)) == 1
        canonical = store.canonical_dataframe()
        assert canonical["location"].to_list() == ["Pusignan, ARA, FR"]
    finally:
        store.close()
