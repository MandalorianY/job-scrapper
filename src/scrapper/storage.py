from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

import duckdb
import pandas as pd

from scrapper.dates import absolute_posted_date
from scrapper.models import JobOffer
from scrapper.text import (
    clean_text,
    completeness_score,
    dedupe_key_for_fields,
    description_dedupe_key_for_fields,
    has_excluded_contract_signal,
    is_application_destination,
    matches_job_title,
    normalize_text,
    stable_hash,
    url_key,
)

SOURCE_DIRECT_URL_IDENTITY_HOST_BLOCKLIST = {
    "jobijoba": "jobijoba.com",
}
OBSERVATION_RETENTION_DAYS = 90


def _offer_url_key(record: Mapping[str, Any]) -> str | None:
    source = str(record.get("source") or "")
    blocked_host = SOURCE_DIRECT_URL_IDENTITY_HOST_BLOCKLIST.get(source)
    if blocked_host:
        direct_url = clean_text(record.get("direct_url"))
        direct_key = _external_url_key(direct_url, blocked_host) if is_application_destination(direct_url) else None
        if direct_key:
            return direct_key
    return url_key(clean_text(record.get("source_url")))


def _external_url_key(url: str | None, blocked_host: str) -> str | None:
    parsed = urlparse(url or "")
    if not parsed.netloc or blocked_host in parsed.netloc.lower():
        return None
    return url_key(url)


GENERIC_LISTING_PATHS = {
    "apply", "apply-now", "application", "career", "careers", "emploi", "emplois", "job", "jobs", "offres",
    "postuler", "redirect", "view",
}


def _listing_link_key(value: str | None) -> str | None:
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    key = url_key(value)
    leaf = parsed.path.rstrip("/").rsplit("/", 1)[-1].lower()
    if key and ("?" in key or (leaf and leaf not in GENERIC_LISTING_PATHS)):
        return key
    return None


def _location_key(value: object | None) -> str:
    city = (clean_text(value) or "").split(",", 1)[0]
    return normalize_text(re.sub(r"\s*\(\d{2,5}\)$", "", city))


def _datetime_value(value: object | None) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = clean_text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class JobStore:
    def __init__(
        self,
        path: Path,
        source_priority: Mapping[str, int] | None = None,
        default_source_priority: int = 100,
        dedupe_primary_fields: tuple[str, ...] = ("title", "company"),
        dedupe_fallback_fields: tuple[str, ...] = ("location",),
        dedupe_description_fields: tuple[str, ...] = ("description", "company", "location"),
        *,
        run_id: str | None = None,
    ) -> None:
        self.path = path
        self.source_priority = {str(source): int(priority) for source, priority in (source_priority or {}).items()}
        self.default_source_priority = default_source_priority
        self.dedupe_primary_fields = tuple(dedupe_primary_fields)
        self.dedupe_fallback_fields = tuple(dedupe_fallback_fields)
        self.dedupe_description_fields = tuple(dedupe_description_fields)
        self.run_id = run_id or str(uuid4())
        self._active_searches: dict[str, str] = {}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(path))
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
        self.conn.execute("CREATE TABLE IF NOT EXISTS excluded_offer_urls (url_key VARCHAR PRIMARY KEY)")
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS raw_offers (
                offer_id VARCHAR PRIMARY KEY,
                dedupe_key VARCHAR NOT NULL,
                description_key VARCHAR,
                source VARCHAR NOT NULL,
                source_id VARCHAR,
                source_url VARCHAR NOT NULL,
                source_url_key VARCHAR,
                direct_url VARCHAR,
                title VARCHAR,
                company VARCHAR,
                location VARCHAR,
                description VARCHAR,
                date_posted VARCHAR,
                employment_type VARCHAR,
                salary VARCHAR,
                min_amount DOUBLE,
                max_amount DOUBLE,
                currency VARCHAR,
                interval VARCHAR,
                is_remote BOOLEAN,
                company_url VARCHAR,
                company_description VARCHAR,
                emails VARCHAR,
                search_query VARCHAR,
                search_location VARCHAR,
                completeness_score INTEGER NOT NULL,
                first_seen_at TIMESTAMPTZ NOT NULL,
                last_seen_at TIMESTAMPTZ NOT NULL,
                scraped_at TIMESTAMPTZ NOT NULL,
                raw_json JSON
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS search_state (
                search_key VARCHAR PRIMARY KEY,
                source VARCHAR NOT NULL,
                query VARCHAR NOT NULL,
                location VARCHAR NOT NULL,
                last_success_at TIMESTAMPTZ,
                last_started_at TIMESTAMPTZ,
                last_seen_count INTEGER DEFAULT 0,
                last_error VARCHAR
            )
            """
        )
        self.conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS raw_offers_url_idx ON raw_offers(source, source_url_key)"
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS offer_observations (
                observation_id VARCHAR PRIMARY KEY,
                run_id VARCHAR NOT NULL,
                observed_at TIMESTAMPTZ NOT NULL,
                payload JSON NOT NULL
            );
            CREATE TABLE IF NOT EXISTS search_attempts (
                attempt_id VARCHAR PRIMARY KEY,
                search_key VARCHAR NOT NULL,
                run_id VARCHAR NOT NULL,
                started_at TIMESTAMPTZ NOT NULL,
                finished_at TIMESTAMPTZ,
                seen_count INTEGER,
                error VARCHAR
            );
            CREATE VIEW IF NOT EXISTS source_health AS
            SELECT state.source, state.query, state.location, attempt.run_id, attempt.started_at,
                   attempt.finished_at, state.last_success_at, attempt.seen_count, attempt.error,
                   CASE WHEN attempt.attempt_id IS NULL THEN 'unknown'
                        WHEN attempt.finished_at IS NULL THEN 'unfinished'
                        WHEN attempt.error IS NOT NULL THEN 'failed'
                        ELSE 'succeeded' END AS status
            FROM search_state AS state
            LEFT JOIN search_attempts AS attempt USING (search_key)
            QUALIFY row_number() OVER (
                PARTITION BY state.search_key ORDER BY attempt.started_at DESC, attempt.attempt_id DESC
            ) = 1
            """
        )
        self._ensure_optional_columns()
        self._normalize_existing_posted_dates()

    def _ensure_optional_columns(self) -> None:
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info('raw_offers')").fetchall()
        }
        if "description_key" not in columns:
            self.conn.execute("ALTER TABLE raw_offers ADD COLUMN description_key VARCHAR")

    def _normalize_existing_posted_dates(self) -> None:
        rows = self.conn.execute(
            """
            SELECT offer_id, date_posted, first_seen_at
            FROM raw_offers
            WHERE date_posted IS NOT NULL
              AND trim(date_posted) != ''
              AND NOT regexp_matches(trim(date_posted), '^\\d{4}-\\d{2}-\\d{2}')
            """
        ).fetchall()
        updates = []
        for offer_id, value, first_seen_at in rows:
            normalized = absolute_posted_date(value, now=first_seen_at)
            if normalized and normalized != clean_text(value):
                updates.append((normalized, offer_id))
        if updates:
            self.conn.executemany(
                "UPDATE raw_offers SET date_posted = ? WHERE offer_id = ?",
                updates,
            )

    def start_search(self, source: str, query: str, location: str) -> str:
        key = self.search_key(source, query, location)
        now = datetime.now(UTC)
        attempt_id = str(uuid4())
        self.conn.begin()
        try:
            self.conn.execute(
                """
                INSERT INTO search_state(search_key, source, query, location, last_started_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(search_key) DO UPDATE SET last_started_at = excluded.last_started_at
                """,
                [key, source, query, location, now],
            )
            self.conn.execute(
                "INSERT INTO search_attempts(attempt_id, search_key, run_id, started_at) VALUES (?, ?, ?, ?)",
                [attempt_id, key, self.run_id, now],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._active_searches[key] = attempt_id
        return key

    def finish_search(self, source: str, query: str, location: str, seen_count: int, error: str | None = None) -> None:
        key = self.search_key(source, query, location)
        attempt_id = self._active_searches.get(key)
        if attempt_id is None:
            return
        now = datetime.now(UTC)
        self.conn.begin()
        try:
            self.conn.execute(
                """
                UPDATE search_state
                SET last_success_at = CASE WHEN ? IS NULL THEN ? ELSE last_success_at END,
                    last_error = ?, last_seen_count = ?
                WHERE search_key = ?
                """,
                [error, now, error, seen_count, key],
            )
            self.conn.execute(
                "UPDATE search_attempts SET finished_at = ?, seen_count = ?, error = ? WHERE attempt_id = ?",
                [now, seen_count, error, attempt_id],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        self._active_searches.pop(key, None)

    def get_last_success(self, source: str, query: str, location: str) -> datetime | None:
        key = self.search_key(source, query, location)
        row = self.conn.execute(
            "SELECT last_success_at FROM search_state WHERE search_key = ?",
            [key],
        ).fetchone()
        return row[0] if row and row[0] else None

    def source_url_seen(self, source: str, source_url: str) -> bool:
        key = url_key(source_url)
        if not key:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM raw_offers WHERE source = ? AND (source_url_key = ? OR source_url = ?) LIMIT 1",
            [source, key, source_url],
        ).fetchone()
        return row is not None

    def upsert_offer(self, offer: JobOffer) -> bool:
        record = offer.to_record()
        observed_at = datetime.now(UTC)
        payload = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True)
        observation_id = sha256(payload.encode()).hexdigest()
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO offer_observations VALUES (?, ?, ?, ?::JSON) RETURNING observation_id",
            [observation_id, self.run_id, observed_at, payload],
        ).fetchone()
        if not inserted:
            observed_at = self.conn.execute(
                "SELECT observed_at FROM offer_observations WHERE observation_id = ?", [observation_id]
            ).fetchone()[0]
        return self._upsert_record(record, observed_at)

    def prune_observations(self) -> None:
        self.conn.execute(
            "DELETE FROM offer_observations WHERE observed_at < now() - INTERVAL (?) DAY",
            [OBSERVATION_RETENTION_DAYS],
        )

    def replay_observations(self, archive: Path) -> int:
        same_db = self.path.resolve() == archive.resolve()
        source = self.conn if same_db else duckdb.connect(str(archive), read_only=True)
        try:
            rows = source.execute(
                """
                SELECT observed_at, payload FROM offer_observations
                WHERE observed_at >= now() - INTERVAL (?) DAY
                ORDER BY observed_at, observation_id
                """,
                [OBSERVATION_RETENTION_DAYS],
            ).fetchall()
        finally:
            if not same_db:
                source.close()
        self.conn.begin()
        try:
            for observed_at, payload in rows:
                self._upsert_record(json.loads(payload), observed_at)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows)

    def _upsert_record(self, record: dict[str, Any], observed_at: datetime) -> bool:
        record["date_posted"] = absolute_posted_date(
            record.get("date_posted"),
            now=_datetime_value(record.get("scraped_at")),
        )
        source_url_key = _offer_url_key(record)
        source = record["source"]
        identity = source_url_key or record["source_url"]
        existing = self.conn.execute(
            "SELECT offer_id FROM raw_offers WHERE source = ? AND source_url_key = ?", [source, identity]
        ).fetchone()
        offer_id = existing[0] if existing else sha256(f"{source}\0{identity}".encode()).hexdigest()
        duplicate_key = dedupe_key_for_fields(
            record,
            fields=self.dedupe_primary_fields,
            fallback_fields=self.dedupe_fallback_fields,
        )
        duplicate_description_key = description_dedupe_key_for_fields(
            record,
            fields=self.dedupe_description_fields,
        )
        record_score = completeness_score(record)
        raw_json = json.dumps(record.get("raw") or {}, ensure_ascii=False, default=str)
        params: list[Any] = [
            offer_id,
            duplicate_key,
            duplicate_description_key,
            record["source"],
            record.get("source_id"),
            record["source_url"],
            source_url_key,
            record.get("direct_url"),
            clean_text(record.get("title")),
            clean_text(record.get("company")),
            clean_text(record.get("location")),
            clean_text(record.get("description")),
            record.get("date_posted"),
            clean_text(record.get("employment_type")),
            clean_text(record.get("salary")),
            record.get("min_amount"),
            record.get("max_amount"),
            record.get("currency"),
            record.get("interval"),
            record.get("is_remote"),
            record.get("company_url"),
            clean_text(record.get("company_description")),
            clean_text(record.get("emails")),
            record.get("search_query"),
            record.get("search_location"),
            record_score,
            observed_at,
            observed_at,
            record.get("scraped_at"),
            raw_json,
        ]
        self.conn.execute(
            """
            INSERT INTO raw_offers (
                offer_id, dedupe_key, description_key, source, source_id, source_url, source_url_key, direct_url,
                title, company, location, description, date_posted, employment_type, salary,
                min_amount, max_amount, currency, interval, is_remote, company_url,
                company_description, emails, search_query, search_location, completeness_score,
                first_seen_at, last_seen_at, scraped_at, raw_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?::JSON)
            ON CONFLICT(offer_id) DO UPDATE SET
                dedupe_key = excluded.dedupe_key,
                description_key = excluded.description_key,
                source_id = COALESCE(excluded.source_id, raw_offers.source_id),
                direct_url = COALESCE(excluded.direct_url, raw_offers.direct_url),
                title = COALESCE(excluded.title, raw_offers.title),
                company = COALESCE(excluded.company, raw_offers.company),
                location = COALESCE(excluded.location, raw_offers.location),
                description = CASE
                    WHEN length(COALESCE(excluded.description, '')) > length(COALESCE(raw_offers.description, ''))
                    THEN excluded.description ELSE raw_offers.description END,
                date_posted = COALESCE(raw_offers.date_posted, excluded.date_posted),
                employment_type = COALESCE(excluded.employment_type, raw_offers.employment_type),
                salary = COALESCE(excluded.salary, raw_offers.salary),
                min_amount = COALESCE(excluded.min_amount, raw_offers.min_amount),
                max_amount = COALESCE(excluded.max_amount, raw_offers.max_amount),
                currency = COALESCE(excluded.currency, raw_offers.currency),
                interval = COALESCE(excluded.interval, raw_offers.interval),
                is_remote = COALESCE(excluded.is_remote, raw_offers.is_remote),
                company_url = COALESCE(excluded.company_url, raw_offers.company_url),
                company_description = COALESCE(excluded.company_description, raw_offers.company_description),
                emails = COALESCE(excluded.emails, raw_offers.emails),
                search_query = COALESCE(excluded.search_query, raw_offers.search_query),
                search_location = COALESCE(excluded.search_location, raw_offers.search_location),
                completeness_score = GREATEST(excluded.completeness_score, raw_offers.completeness_score),
                last_seen_at = excluded.last_seen_at,
                scraped_at = excluded.scraped_at,
                raw_json = excluded.raw_json
            WHERE excluded.last_seen_at >= raw_offers.last_seen_at
            """,
            params,
        )
        # An accepted, freshly scraped offer can replace a previously excluded listing.
        for link in (record.get("source_url"), record.get("direct_url")):
            if key := url_key(link):
                self.conn.execute("DELETE FROM excluded_offer_urls WHERE url_key = ?", [key])
        return True

    def canonical_dataframe(self):
        frame = self.conn.execute(
            """
            SELECT
                offer_id,
                dedupe_key,
                description_key,
                source,
                source_id,
                source_url,
                direct_url,
                title,
                company,
                location,
                description,
                date_posted,
                employment_type,
                salary,
                min_amount,
                max_amount,
                currency,
                interval,
                is_remote,
                company_url,
                company_description,
                emails,
                search_query,
                search_location,
                completeness_score,
                first_seen_at,
                last_seen_at,
                scraped_at,
                raw_json
            FROM raw_offers
            ORDER BY offer_id
            """
        ).df()
        if frame.empty:
            return pd.DataFrame(
                columns=[
                    "title",
                    "company",
                    "location",
                    "date_posted",
                    "employment_type",
                    "salary",
                    "min_amount",
                    "max_amount",
                    "currency",
                    "interval",
                    "is_remote",
                    "best_source",
                    "best_source_url",
                    "best_direct_url",
                    "sources",
                    "all_source_links",
                    "all_direct_links",
                    "duplicate_count",
                    "description",
                    "company_url",
                    "company_description",
                    "emails",
                    "search_query",
                    "search_location",
                    "completeness_score",
                    "first_seen_at",
                    "last_seen_at",
                    "raw_json",
                ]
            )

        frame["date_posted"] = frame.apply(
            lambda row: absolute_posted_date(row["date_posted"], now=row["first_seen_at"]),
            axis=1,
        )
        frame["direct_url"] = frame["direct_url"].where(frame["direct_url"].map(is_application_destination), None)
        frame["group_key"] = self._build_group_keys(frame)
        frame["source_priority"] = frame["source"].map(self.source_priority).fillna(self.default_source_priority).astype(int)

        candidates = frame[frame["title"].notna() & frame["company"].notna()].copy()
        if candidates.empty:
            return pd.DataFrame(
                columns=[
                    "title",
                    "company",
                    "location",
                    "date_posted",
                    "employment_type",
                    "salary",
                    "min_amount",
                    "max_amount",
                    "currency",
                    "interval",
                    "is_remote",
                    "best_source",
                    "best_source_url",
                    "best_direct_url",
                    "sources",
                    "all_source_links",
                    "all_direct_links",
                    "duplicate_count",
                    "description",
                    "company_url",
                    "company_description",
                    "emails",
                    "search_query",
                    "search_location",
                    "completeness_score",
                    "first_seen_at",
                    "last_seen_at",
                    "raw_json",
                ]
            )

        candidates = candidates.sort_values(
            by=["group_key", "source_priority", "completeness_score", "scraped_at"],
            ascending=[True, True, False, False],
            kind="stable",
        )
        best_rows = candidates.drop_duplicates(subset=["group_key"], keep="first").copy()
        best_rows = best_rows.drop(columns=["first_seen_at", "last_seen_at"], errors="ignore")

        links = (
            frame.groupby("group_key", sort=False)
            .apply(self._aggregate_group_links, include_groups=False)
            .reset_index()
        )
        result = best_rows.merge(links, on="group_key", how="inner")
        result["description"] = result.pop("group_description")
        result["date_posted"] = result["date_posted"].where(
            result["date_posted"].notna() & (result["date_posted"].astype(str).str.strip() != ""),
            result["group_date_posted"],
        )
        result["sort_date"] = pd.to_datetime(result["date_posted"], errors="coerce", utc=True)
        result["sort_date"] = result["sort_date"].fillna(
            pd.to_datetime(result["last_seen_at"], errors="coerce", utc=True)
        )
        result = result.sort_values(by=["sort_date", "company", "title"], ascending=[False, True, True], kind="stable")

        return result.rename(
            columns={
                "source": "best_source",
                "source_url": "best_source_url",
                "direct_url": "best_direct_url",
            }
        )[
            [
                "title",
                "company",
                "location",
                "date_posted",
                "employment_type",
                "salary",
                "min_amount",
                "max_amount",
                "currency",
                "interval",
                "is_remote",
                "best_source",
                "best_source_url",
                "best_direct_url",
                "sources",
                "all_source_links",
                "all_direct_links",
                "duplicate_count",
                "description",
                "company_url",
                "company_description",
                "emails",
                "search_query",
                "search_location",
                "completeness_score",
                "first_seen_at",
                "last_seen_at",
                "raw_json",
            ]
        ]

    def _build_group_keys(self, frame: pd.DataFrame) -> pd.Series:
        parents = list(range(len(frame)))
        rows = list(frame.itertuples(index=False))
        sources = [{row.source} for row in rows]
        locations = [{key} if (key := _location_key(row.location)) else set() for row in rows]
        use_location = any("location" in fields for fields in (
            self.dedupe_primary_fields, self.dedupe_fallback_fields, self.dedupe_description_fields
        ))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            parents[right] = left
            sources[left].update(sources[right])
            locations[left].update(locations[right])

        seen_identity: dict[tuple[str, ...], int] = {}
        for index, row in enumerate(rows):
            direct_url = row.direct_url if is_application_destination(row.direct_url) else None
            keys = [("url", key) for link in (row.source_url, direct_url) if (key := _listing_link_key(link))]
            if row.source_id and str(row.source_id).lower() not in GENERIC_LISTING_PATHS:
                host = (urlparse(row.source_url).hostname or "").removeprefix("www.")
                keys.append(("id", row.source, host, str(row.source_id)))
            for key in keys:
                left, right = find(index), find(seen_identity.setdefault(key, index))
                if left != right:
                    union(left, right)

        candidates: dict[tuple[str, str], set[int]] = {}
        for index, row in enumerate(rows):
            description_key = row.description_key or description_dedupe_key_for_fields(
                row._asdict(), fields=self.dedupe_description_fields,
            )
            for kind, value in (("title", row.dedupe_key), ("description", description_key)):
                if not value:
                    continue
                bucket = candidates.setdefault((kind, value), set())
                for previous in sorted(bucket):
                    left, right = find(index), find(previous)
                    if left == right or sources[left] & sources[right]:
                        continue
                    if use_location and locations[left] and locations[right] and locations[left].isdisjoint(locations[right]):
                        continue
                    union(left, right)
                bucket.add(find(index))

        return pd.Series((str(find(index)) for index in range(len(frame))), index=frame.index, dtype="string")

    @staticmethod
    def _aggregate_group_links(group: pd.DataFrame) -> pd.Series:
        return pd.Series(
            {
                "sources": ", ".join(sorted({value for value in group["source"].dropna() if value})),
                "all_source_links": "\n".join(sorted({value for value in group["source_url"].dropna() if value})),
                "all_direct_links": JobStore._join_optional_links(group["direct_url"]),
                "group_description": max(group["description"].dropna(), key=len, default=None),
                "group_date_posted": JobStore._best_group_value(group, "date_posted"),
                "duplicate_count": len(group),
                "first_seen_at": group["first_seen_at"].min(),
                "last_seen_at": group["last_seen_at"].max(),
            }
        )

    @staticmethod
    def _best_group_value(group: pd.DataFrame, column: str) -> str | None:
        if column not in group.columns:
            return None
        ranked = group.sort_values(
            by=["source_priority", "completeness_score", "scraped_at"],
            ascending=[True, False, False],
            kind="stable",
        )
        for value in ranked[column]:
            if value is None:
                continue
            text = str(value).strip()
            if text and text.lower() not in {"nan", "none", "nat"}:
                return text
        return None

    @staticmethod
    def _join_optional_links(values: pd.Series) -> str | None:
        deduped = sorted({value for value in values.dropna() if value})
        if not deduped:
            return None
        return "\n".join(deduped)

    def raw_dataframe(self):
        return self.conn.execute(
            """
            SELECT
                source, title, company, location, date_posted, employment_type, salary,
                min_amount, max_amount, currency, interval, is_remote, source_url, direct_url,
                description, company_url, company_description, emails, search_query,
                search_location, completeness_score, first_seen_at, last_seen_at, raw_json
            FROM raw_offers
            ORDER BY last_seen_at DESC
            """
        ).df()

    def purge_excluded_offers(self, exclude_terms: tuple[str, ...]) -> int:
        frame = self.conn.execute(
            """
            SELECT offer_id, dedupe_key, description_key, source, source_id, source_url, direct_url,
                   location, title, employment_type, description
            FROM raw_offers ORDER BY offer_id
            """
        ).df()
        if frame.empty:
            return 0
        rejected = frame.apply(
            lambda row: has_excluded_contract_signal(
                row["title"], row["employment_type"], row["description"], exclude_terms
            ), axis=1,
        )
        # A sparse copy must not hide the contract disclosed by another board for
        # the same offer. Use the same identity groups as the canonical export.
        groups = self._build_group_keys(frame)
        rejected_groups = set(groups[rejected])
        offer_ids = frame.loc[groups.isin(rejected_groups), "offer_id"].tolist()
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def purge_nonmatching_offers(
        self, include_terms: tuple[str, ...], excluded_title_patterns: tuple[str, ...] = ()
    ) -> int:
        rows = self.conn.execute(
            "SELECT offer_id, title FROM raw_offers"
        ).fetchall()
        offer_ids = [
            offer_id
            for offer_id, title in rows
            if not matches_job_title(title, include_terms, excluded_title_patterns)
        ]
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def purge_out_of_scope_offers(self, location_allowed: Callable[[object | None], bool]) -> int:
        rows = self.conn.execute(
            "SELECT offer_id, location FROM raw_offers"
        ).fetchall()
        offer_ids = [
            offer_id
            for offer_id, location in rows
            if not location_allowed(location)
        ]
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def purge_excluded_companies(self, excluded_companies_by_source: Mapping[str, tuple[str, ...]]) -> int:
        excluded = {
            str(source): {normalize_text(company) for company in companies if normalize_text(company)}
            for source, companies in excluded_companies_by_source.items()
        }
        if not excluded:
            return 0
        global_excluded = excluded.get("*", set())
        rows = self.conn.execute(
            "SELECT offer_id, source, company FROM raw_offers"
        ).fetchall()
        offer_ids = []
        for offer_id, source, company in rows:
            normalized_company = normalize_text(company)
            if not normalized_company:
                continue
            source_excluded = excluded.get(str(source), set())
            if normalized_company in source_excluded or normalized_company in global_excluded:
                offer_ids.append(offer_id)
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def _delete_offer_ids(self, offer_ids: list[str]) -> None:
        rows = self.conn.execute(
            "SELECT source_url, direct_url FROM raw_offers WHERE offer_id IN (SELECT unnest(?))", [offer_ids]
        ).fetchall()
        keys = {(url_key(link),) for row in rows for link in row if link}
        if keys:
            self.conn.executemany("INSERT OR IGNORE INTO excluded_offer_urls VALUES (?)", list(keys))
        params = [(offer_id,) for offer_id in offer_ids]
        try:
            self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
        except duckdb.Error:
            self.conn.execute("DROP INDEX IF EXISTS raw_offers_url_idx")
            try:
                self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
            finally:
                self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS raw_offers_url_idx ON raw_offers(source, source_url_key)")

    def excluded_url_keys(self) -> set[str]:
        """Prevent old tracker copies from restoring offers removed by a purge."""
        return {row[0] for row in self.conn.execute("SELECT url_key FROM excluded_offer_urls").fetchall()}

    @staticmethod
    def search_key(source: str, query: str, location: str) -> str:
        return stable_hash(source, query, location)
