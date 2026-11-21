from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from scrapper.config import DEFAULT_EXCLUDE_TERMS, DEFAULT_INCLUDE_TERMS, SearchConfig
from scrapper.config.settings import LocationScopeConfig
from scrapper.location_scope import location_is_in_scope
from scrapper.models import JobOffer
from scrapper.storage import JobStore


def test_bad_apply_links_do_not_merge_jobs_or_replace_source_links(tmp_path: Path) -> None:
    store = JobStore(tmp_path / "jobs.duckdb", source_priority={"hellowork": 0})
    first = "https://www.hellowork.com/fr-fr/emplois/1.html"
    second = "https://www.hellowork.com/fr-fr/emplois/2.html"
    bookmark = "https://www.hellowork.com/fr-fr/candidat/tooglebookmark"
    try:
        for title, url in [("Example coordinator", first), ("Demo specialist", second)]:
            store.upsert_offer(
                JobOffer(
                    source="hellowork",
                    source_url=url,
                    title=title,
                    company="Example",
                    location="Toulouse",
                    direct_url=bookmark,
                    description="Résumé court",
                )
            )
        store.upsert_offer(
            JobOffer(
                source="partner",
                source_url="https://example.com/jobs/1",
                direct_url=first,
                title="Example coordinator",
                company="Example",
                location="Toulouse",
                description="Description complète avec les missions et les qualifications du poste.",
            )
        )

        canonical = store.canonical_dataframe().set_index("best_source_url")

        assert set(canonical.index) == {first, second}
        assert canonical.loc[first, "duplicate_count"] == 2
        assert canonical.loc[first, "description"].startswith("Description complète")
        assert canonical.loc[first, "all_direct_links"] == first
        assert canonical.loc[:, "best_direct_url"].isna().all()
    finally:
        store.close()


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
        assert store.purge_excluded_offers(("stage",)) == 2
        assert store.raw_dataframe()["company"].tolist() == ["Other employer"]
        assert len(store.excluded_url_keys()) == 2
    finally:
        store.close()


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
                scraped_at="2026-06-02T12:00:00+00:00",
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
                scraped_at="2026-06-01T12:00:00+00:00",
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
    finally:
        store.close()


def test_storage_ranks_configured_last_source_behind_unlisted_sources(tmp_path: Path) -> None:
    config = SearchConfig()
    store = _store(
        tmp_path,
        source_priority=config.dedupe_source_priority,
        default_source_priority=config.dedupe_default_source_priority,
    )
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


def test_listing_identity_beats_title_company_matches(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for source, listing, location, apply in (
            ("board", "toulouse-1", "Toulouse", "https://employer.example/jobs/432?utm_source=board"),
            ("board", "grenoble-1", "Grenoble", "https://employer.example/careers"),
            ("board", "toulouse-2", "Toulouse", "https://employer.example/careers"),
            ("partner", "different-title", "Toulouse, France", "https://employer.example/jobs/432"),
            ("third-board", "annecy-1", "Annecy", None),
        ):
            store.upsert_offer(
                JobOffer(
                    source=source,
                    source_id=listing,
                    source_url=f"https://{source}.example/jobs/{listing}",
                    title="Analyste confirmé" if source == "partner" else "Data Analyst",
                    company="Same employer",
                    location=location,
                    direct_url=apply,
                )
            )
        # A provider can change a detail URL while keeping the listing ID.
        store.upsert_offer(
            JobOffer(
                source="board",
                source_id="toulouse-1",
                source_url="https://board.example/jobs/renamed",
                title="Data Analyst",
                company="Same employer",
                location="Toulouse",
            )
        )
        canonical = store.canonical_dataframe()
        assert sorted(canonical["duplicate_count"].tolist()) == [1, 1, 1, 3]
        assert set(canonical["location"]) >= {"Toulouse", "Grenoble", "Annecy"}
    finally:
        store.close()


def test_shared_apply_pages_cannot_merge_distinct_requisitions(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for index, direct in enumerate(
            (
                "https://www.francetravail.fr/a/article.jspz?id=1",
                "https://www.francetravail.fr/a/article.jspz?id=1",
                "https://recrutement.lyon.fr/front-jobs-detail.html?id_job=10&id_origin=1",
                "https://recrutement.lyon.fr/front-jobs-detail.html?id_job=20&id_origin=2",
                "https://recrutement.lyon.fr/front-jobs-detail.html?id_job=20&id_origin=3",
            )
        ):
            store.upsert_offer(
                JobOffer(
                    source="board",
                    source_url=f"https://board.example/jobs/{index}",
                    direct_url=direct,
                    title=f"Chargé de communication {min(index, 2)}",
                    company="Example employer",
                    location="Lyon",
                    description="We are hiring a communications coordinator with experience running regional campaigns.",
                )
            )
        canonical = store.canonical_dataframe()
        assert sorted(canonical["duplicate_count"]) == [1, 1, 1, 2]
        assert canonical["best_direct_url"].isna().sum() == 2
    finally:
        store.close()


def test_refresh_keeps_legacy_identity_after_normalizing_job_id_parameter(tmp_path: Path) -> None:
    store = _store(tmp_path)
    offer = JobOffer(
        source="board",
        source_url="https://recrutement.lyon.fr/front-jobs-detail.html?id_job=10",
        title="Chargé de communication",
        company="Example employer",
    )
    try:
        store.upsert_offer(offer)
        store.conn.execute("UPDATE raw_offers SET source_url_key = 'recrutement.lyon.fr/front-jobs-detail.html'")
        original = store.conn.execute("SELECT offer_id, first_seen_at FROM raw_offers").fetchall()
        store.upsert_offer(offer)
        assert store.conn.execute("SELECT offer_id, first_seen_at FROM raw_offers").fetchall() == original
        store.upsert_offer(
            JobOffer(
                source="board",
                source_url="https://recrutement.lyon.fr/front-jobs-detail.html?id_job=20",
                title="Webmaster",
                company="Example employer",
            )
        )
        assert len(store.raw_dataframe()) == 2
    finally:
        store.close()


def test_same_board_reposts_need_matching_title_company_location_and_description(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for index, town in enumerate(("Lyon", "Lyon", "Grenoble")):
            store.upsert_offer(
                JobOffer(
                    source="board",
                    source_url=f"https://board.example/jobs/{index}",
                    title="Chargé de communication",
                    company="Example employer",
                    location=town,
                    description="We are hiring a communications coordinator with experience running regional campaigns.",
                )
            )
        assert sorted(store.canonical_dataframe()["duplicate_count"]) == [1, 2]
        assert len(store.raw_dataframe()) == 3
    finally:
        store.close()


def test_search_attempts_distinguish_empty_failed_and_unfinished(tmp_path: Path) -> None:
    store = _store(tmp_path)
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


def test_source_url_seen_uses_normalized_url_key(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="meteojob",
                source_url="https://company.example/jobs/comms?utm_source=google",
                title="Example coordinator",
                company="Example",
            )
        )

        assert store.source_url_seen("meteojob", "https://company.example/jobs/comms?utm_campaign=other")
    finally:
        store.close()


def test_jobijoba_uses_external_apply_url_as_stable_identity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for wrapper_url, date_posted, scraped_at in (
            ("https://www.jobijoba.com/fr/annonce/54/old-wrapper", "Il y a 2 jours", "2026-06-05T12:00:00Z"),
            ("https://www.jobijoba.com/fr/annonce/54/new-wrapper", "Il y a 1 h", "2026-06-06T12:00:00Z"),
        ):
            store.upsert_offer(
                JobOffer(
                    source="jobijoba",
                    source_id="www.hellowork.com/fr-fr/emplois/79739983.html",
                    source_url=wrapper_url,
                    direct_url="https://www.hellowork.com/fr-fr/emplois/79739983.html?utm_source=jobijoba",
                    title="Example coordinator",
                    company="Example",
                    location="Toulouse",
                    date_posted=date_posted,
                    scraped_at=scraped_at,
                )
            )

        rows = store.conn.execute(
            "SELECT source_url, source_url_key, date_posted, first_seen_at, last_seen_at FROM raw_offers"
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "https://www.jobijoba.com/fr/annonce/54/old-wrapper"
        assert rows[0][1] == "www.hellowork.com/fr-fr/emplois/79739983.html"
        assert rows[0][2] == "2026-06-03T12:00:00+00:00"
        assert rows[0][3] <= rows[0][4]
        assert store.source_url_seen("jobijoba", "https://www.jobijoba.com/fr/annonce/54/old-wrapper")
    finally:
        store.close()


def test_storage_migrates_relative_dates_using_first_seen_time(tmp_path: Path) -> None:
    path = tmp_path / "jobs.duckdb"
    store = JobStore(path)
    try:
        store.upsert_offer(
            JobOffer(
                source="jobijoba",
                source_url="https://www.jobijoba.com/fr/annonce/54/stale-wrapper",
                title="Old example coordinator",
                company="Example",
                location="Toulouse",
                date_posted="2026-06-30",
            )
        )
        store.conn.execute(
            """
            UPDATE raw_offers
            SET date_posted = 'Il y a 19 h',
                first_seen_at = TIMESTAMPTZ '2026-07-01 16:00:00+02:00'
            """
        )
    finally:
        store.close()

    migrated = JobStore(path)
    try:
        row = migrated.conn.execute("SELECT date_posted FROM raw_offers").fetchone()
        assert row is not None
        (date_posted,) = row
        assert datetime.fromisoformat(date_posted) == datetime(2026, 6, 30, 19, tzinfo=UTC)
    finally:
        migrated.close()


def test_storage_sorts_absolute_dates_instead_of_relative_date_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="jobijoba",
                source_url="https://www.jobijoba.com/fr/annonce/54/old",
                title="Old example coordinator",
                company="Old Example",
                location="Toulouse",
                date_posted="Il y a 19 h",
                scraped_at="2026-07-01T16:00:00+02:00",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="linkedin",
                source_url="https://www.linkedin.com/jobs/view/new",
                title="New example coordinator",
                company="New Example",
                location="Toulouse",
                date_posted="2026-07-22T09:00:00+02:00",
            )
        )

        canonical = store.canonical_dataframe()

        assert canonical["title"].to_list() == [
            "New example coordinator",
            "Old example coordinator",
        ]
        assert canonical.iloc[1]["date_posted"] == "2026-06-30T21:00:00+02:00"
    finally:
        store.close()


def test_upsert_preserves_first_known_posted_date(tmp_path: Path) -> None:
    store = _store(tmp_path)
    try:
        for date_posted in ("2026-06-01", "2026-06-25"):
            store.upsert_offer(
                JobOffer(
                    source="test",
                    source_url="https://example.com/jobs/1",
                    title="Example coordinator",
                    company="Example",
                    location="Toulouse",
                    date_posted=date_posted,
                )
            )

        assert store.conn.execute("SELECT date_posted FROM raw_offers").fetchone() == ("2026-06-01",)
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


def test_purge_excluded_companies_removes_source_specific_company(tmp_path: Path) -> None:
    store = _store(tmp_path)
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

        assert store.purge_excluded_companies({"linkedin": ("France Travail",)}) == 1

        remaining = store.conn.execute("SELECT source FROM raw_offers").fetchall()
        assert remaining == [("indeed",)]
    finally:
        store.close()


def test_purge_out_of_scope_offers_removes_locations_outside_configured_radius(
    tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    store = _store(tmp_path)
    try:
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/toulouse",
                title="Example coordinator",
                company="Example",
                location="Blagnac, Occitanie, FR",
            )
        )
        store.upsert_offer(
            JobOffer(
                source="test",
                source_url="https://example.com/montauban",
                title="Example coordinator",
                company="Example",
                location="Montauban, Occitanie, France",
            )
        )

        assert store.purge_out_of_scope_offers(lambda location: location_is_in_scope(location, toulouse_scope)) == 1
        canonical = store.canonical_dataframe()
        assert canonical["location"].to_list() == ["Blagnac, Occitanie, FR"]
    finally:
        store.close()
