from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import duckdb
import pandas as pd
import pytest
from typer.testing import CliRunner

from jobsfr.cli import app
from jobsfr.config import SearchConfig
from jobsfr.config.settings import LocationScopeConfig
from jobsfr.export import export_job_formats
from jobsfr.geography.regions import save_location_scope
from jobsfr.geography.scope import location_is_allowed
from jobsfr.offers.models import JobOffer
from jobsfr.scrapers.jobspy_sources import JOBSPY_SITES, scrape, scrape_jobspy_sources
from jobsfr.storage import JobStore


def test_salary_refresh_updates_the_amount_and_unit_together(tmp_path: Path) -> None:
    offer = JobOffer(
        source="indeed",
        source_url="https://indeed.example/jobs/1",
        title="Example coordinator",
        company="Example employer",
        min_amount=50000,
        max_amount=55000,
        interval="year",
        currency="EUR",
    )
    with closing(JobStore(tmp_path / "jobs.duckdb")) as store:
        store.upsert_offer(offer)
        store.upsert_offer(replace(offer, min_amount=None, max_amount=None, interval="month", currency=None))
        assert store.canonical_dataframe()["annual_salary_bounds"].tolist() == [(50000, 55000)]
        store.upsert_offer(replace(offer, min_amount=4000, max_amount=None, interval="month"))
        assert store.canonical_dataframe()["annual_salary_bounds"].tolist() == [(48000, None)]


def test_failed_offer_write_leaves_no_observation_to_poison_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive_path = tmp_path / "archive.duckdb"
    offer = JobOffer(source="indeed", source_url="https://indeed.example/jobs/1")
    with closing(JobStore(archive_path)) as archive:
        upsert_record = archive._upsert_record

        def fail_after_write(record: dict[str, object], observed_at: datetime) -> bool:
            upsert_record(record, observed_at)
            raise duckdb.ConversionException("write failed")

        with monkeypatch.context() as patch:
            patch.setattr(archive, "_upsert_record", fail_after_write)
            with pytest.raises(duckdb.ConversionException, match="write failed"):
                archive.upsert_offer(offer)
        assert archive.raw_dataframe().empty
        assert archive.conn.execute("SELECT count(*) FROM offer_observations").fetchone() == (0,)
        archive.upsert_offer(replace(offer, source_url="https://indeed.example/jobs/2"))

    with closing(JobStore(tmp_path / "rebuilt.duckdb")) as rebuilt:
        assert rebuilt.replay_observations(archive_path) == 1
        assert rebuilt.raw_dataframe()["source_url"].tolist() == ["https://indeed.example/jobs/2"]


def test_jobspy_archives_rejections_for_contract_checks_and_wider_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toulouse_scope: LocationScopeConfig
) -> None:
    config = SearchConfig(
        queries=("example role",),
        locations=("Toulouse, France",),
        location_scope=toulouse_scope,
        filters={"title": {"include": ["example coordinator"]}},
        enabled_sources={site: site in {"indeed", "glassdoor"} for site in JOBSPY_SITES},
    )
    common = {"company": "Example employer", "location": "Toulouse", "description": "Coordinate projects."}
    copies = {
        **common,
        "job_url_direct": "https://employer.example/jobs/shared-123",
        "title": "Example coordinator",
        "job_type": "CDI",
    }
    expired_url = "https://www.indeed.fr/viewjob?jk=expired"

    def fake_scrape_jobs(*, site_name: str | list[str], **_kwargs: object) -> pd.DataFrame:
        if site_name == "glassdoor":
            return pd.DataFrame(
                [
                    {
                        **copies,
                        "job_url": "https://glassdoor.example/jobs/1",
                        "job_level": "Stage",
                        "description": None,
                        "job_type": None,
                    }
                ]
            )
        assert site_name == "indeed"
        return pd.DataFrame(
            [
                {**common, "job_url": "not a URL", "title": "Example coordinator"},
                {**copies, "job_url": "https://indeed.example/jobs/1"},
                {**common, "job_url": "https://indeed.example/jobs/2", "title": "Analyste"},
                {**common, "job_url": expired_url, "title": "Analyste expiré"},
            ]
        )

    monkeypatch.setattr(scrape, "scrape_jobs", fake_scrape_jobs)
    monkeypatch.setattr(
        scrape, "_is_expired_indeed_offer", lambda _client, _checked, offer: offer.source_url == expired_url
    )
    archive_path = tmp_path / "archive.duckdb"
    with closing(JobStore(archive_path)) as archive:
        assert scrape_jobspy_sources(config, archive) == {"glassdoor": 0, "indeed": 1}
        assert archive.rejection_counts() == [
            {"source": "glassdoor", "reason": "contract", "count": 1},
            {"source": "indeed", "reason": "filters", "count": 1},
        ]
        assert archive.purge_excluded_offers() == 1
        assert archive.raw_dataframe().empty

    profile = tmp_path / "wide.yaml"
    profile.write_text("location_scope: {enabled: false}\nexport: {formats: [csv]}\n", encoding="utf-8")
    output = tmp_path / "exports"
    result = CliRunner().invoke(
        app,
        [
            "replay",
            "--archive",
            str(archive_path),
            "--profile",
            str(profile),
            "--db",
            str(tmp_path / "rebuilt.duckdb"),
            "--export-dir",
            str(output),
            "--log-file",
            str(tmp_path / "run.jsonl"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert pd.read_csv(output / "jobs.csv")["Intitulé"].tolist() == ["Analyste"]


def test_fresh_contract_observation_supersedes_rejected_url_variant(tmp_path: Path) -> None:
    offer = JobOffer(
        source="jobijoba",
        source_url="https://www.jobijoba.com/fr/annonce/1",
        title="Example coordinator",
        company="Example employer",
        employment_type="Stage",
    )
    with closing(JobStore(tmp_path / "jobs.duckdb")) as store:
        assert not store.upsert_offer(offer, rejection="contract")
        assert store.purge_excluded_offers() == 0
        assert store.excluded_url_keys() == {"www.jobijoba.com/fr/annonce/1"}
        store.upsert_offer(
            replace(
                offer,
                source_url=f"{offer.source_url}?utm_source=refresh",
                direct_url="https://employer.example/jobs/shared-123",
                employment_type="CDI",
            )
        )
        assert store.purge_excluded_offers() == 0
        assert not store.excluded_url_keys()
        assert store.raw_dataframe()["employment_type"].tolist() == ["CDI"]


def test_provider_id_supersedes_contract_evidence_after_url_slug_changes(tmp_path: Path) -> None:
    offer = JobOffer(
        source="linkedin",
        source_id="1234567890",
        source_url="https://www.linkedin.com/jobs/view/analyst-1234567890",
        title="Example coordinator",
        company="Example employer",
        employment_type="Stage",
    )
    with closing(JobStore(tmp_path / "jobs.duckdb")) as store:
        store.upsert_offer(offer, rejection="contract")
        store.purge_excluded_offers()
        corrected = replace(
            offer,
            source_url="https://www.linkedin.com/jobs/view/data-analyst-1234567890",
            employment_type="CDI",
        )
        store.upsert_offer(corrected)
        assert store.purge_excluded_offers() == 0
        assert store.raw_dataframe()["source_url"].tolist() == [corrected.source_url]


def test_replay_applies_retained_exclusions_to_existing_offers(tmp_path: Path) -> None:
    archive_path = tmp_path / "archive.duckdb"
    apply_url = "https://employer.example/jobs/shared-123"
    offer = JobOffer(
        source="indeed",
        source_url="https://indeed.example/jobs/1",
        direct_url=apply_url,
        title="Example coordinator",
        company="Example employer",
        employment_type="CDI",
    )
    with closing(JobStore(archive_path)) as archive:
        archive.upsert_offer(
            replace(offer, source="linkedin", source_url="https://linkedin.example/jobs/1", employment_type="Stage"),
            rejection="contract",
        )
        archive.purge_excluded_offers()
        archive.conn.execute("UPDATE offer_observations SET observed_at = now() - INTERVAL 91 DAY")
        archive.prune_observations()
    with closing(JobStore(tmp_path / "rebuilt.duckdb")) as rebuilt:
        rebuilt.upsert_offer(offer)
        assert rebuilt.replay_observations(archive_path) == 0
        assert rebuilt.purge_excluded_offers() == 1
        assert rebuilt.raw_dataframe().empty
        assert "employer.example/jobs/shared-123" in rebuilt.excluded_url_keys()


def test_export_from_rebuilt_database_uses_replayed_scope(tmp_path: Path, toulouse_scope: LocationScopeConfig) -> None:
    archive_path = tmp_path / "archive" / "jobs.duckdb"
    rebuild_path = tmp_path / "rebuilt" / "jobs.duckdb"
    profile = tmp_path / "toulouse.yaml"
    profile.write_text(
        'locations: ["Toulouse, France"]\nlocation_scope: {include_unrecognized: false}\nexport: {formats: [csv]}\n',
        encoding="utf-8",
    )
    with closing(JobStore(archive_path)) as archive:
        for index, city in enumerate(("Toulouse", "Montauban"), 1):
            archive.upsert_offer(
                JobOffer(
                    source="indeed",
                    source_url=f"https://indeed.example/jobs/{index}",
                    location=city,
                    title="Example coordinator",
                    company="Example employer",
                    employment_type="CDI",
                )
            )
    config = SearchConfig(_profile=profile, paths={"database_path": archive_path})
    save_location_scope(config, toulouse_scope)
    output = tmp_path / "exports"
    common = [
        "--profile",
        str(profile),
        "--db",
        str(rebuild_path),
        "--export-dir",
        str(output),
        "--log-file",
        str(tmp_path / "run.jsonl"),
    ]
    for command in (["replay", "--archive", str(archive_path)], ["export"]):
        result = CliRunner().invoke(app, [*command, *common])
        assert result.exit_code == 0, result.output
        assert pd.read_csv(output / "jobs.csv")["Lieu"].tolist() == ["Toulouse"]


def test_jobspy_result_limit_preserves_the_previous_watermark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, toulouse_scope: LocationScopeConfig
) -> None:
    jobs = pd.DataFrame(
        [
            {
                "job_url": "https://indeed.example/jobs/1",
                "title": "Example coordinator",
                "company": "Example employer",
                "location": "Toulouse",
                "description": "Coordinate projects.",
            }
        ]
    )
    monkeypatch.setattr(scrape, "scrape_jobs", lambda **_: jobs)
    query, location = "example role", "Toulouse, France"
    config = SearchConfig(
        queries=(query,),
        locations=(location,),
        location_scope=toulouse_scope,
        enabled_sources={site: site == "indeed" for site in JOBSPY_SITES},
        results_wanted_per_query=1,
    )
    with closing(JobStore(tmp_path / "jobs.duckdb")) as store:
        store.start_search("indeed", query, location)
        store.finish_search("indeed", query, location, 0)
        watermark = store.get_last_success("indeed", query, location)
        assert scrape_jobspy_sources(config, store) == {"indeed": 1}
        assert store.get_last_success("indeed", query, location) == watermark
        outcome = store.conn.execute("SELECT error FROM source_health WHERE source = 'indeed'").fetchone()
        assert outcome is not None and "coverage unconfirmed" in outcome[0]


def test_export_ranks_in_scope_and_keeps_tracker_status_and_links(
    tmp_path: Path, toulouse_scope: LocationScopeConfig
) -> None:
    columns = ("Statut", "Lien", "Lieu", "Sources", "Offre source", "Autres liens")
    export_dir = tmp_path / "exports"
    with closing(JobStore(tmp_path / "jobs.duckdb", source_priority={"linkedin": 1, "indeed": 2})) as store:
        for source, city in (("linkedin", "Montauban"), ("indeed", "Toulouse")):
            store.upsert_offer(
                JobOffer(
                    source=source,
                    source_url=f"https://{source}.example/jobs/1",
                    direct_url="https://employer.example/jobs/shared-123",
                    title="Example coordinator",
                    company="Example employer",
                    location=city,
                    employment_type="CDI",
                )
            )
        direct_links = {"linkedin": False, "indeed": False}
        (tracker, _), _ = export_job_formats(
            store, export_dir, columns, formats=("csv",), direct_links_enabled_by_source=direct_links
        )
        edited = pd.read_csv(tracker)
        assert edited["Lieu"].tolist() == ["Montauban"]
        edited["Statut"] = "Postulé"
        edited.to_csv(tracker, index=False)
        _, total = export_job_formats(
            store,
            export_dir,
            columns,
            formats=("csv",),
            location_allowed=lambda value: location_is_allowed(value, toulouse_scope),
            direct_links_enabled_by_source=direct_links,
        )
        assert total == 1
        output = pd.read_csv(tracker).iloc[0]
        assert output["Lieu"] == "Toulouse"
        assert output["Statut"] == "Postulé"
        assert output["Sources"] == "indeed, linkedin"
        assert "https://indeed.example/jobs/1" in output["Offre source"]
