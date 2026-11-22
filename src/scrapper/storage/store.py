from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import duckdb
import pandas as pd

from scrapper.dates import absolute_posted_date
from scrapper.models import JobOffer
from scrapper.source_rules.cadremploi import offer_id as cadremploi_offer_id
from scrapper.storage.dedupe import aggregate_group_links, build_group_keys
from scrapper.storage.schema import ensure_schema
from scrapper.text import (
    clean_text,
    completeness_score,
    dedupe_key_for_fields,
    description_dedupe_key_for_fields,
    external_url_key,
    has_excluded_contract_signal,
    is_application_destination,
    offer_link_keys,
    stable_hash,
    url_key,
)

SOURCE_DIRECT_URL_IDENTITY_HOST_BLOCKLIST = {
    "jobijoba": "jobijoba.com",
}
OBSERVATION_RETENTION_DAYS = 90
CANONICAL_COLUMNS = [
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
    "status_alias_keys",
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


def _offer_url_key(record: Mapping[str, object]) -> str | None:
    source = str(record.get("source") or "")
    blocked_host = SOURCE_DIRECT_URL_IDENTITY_HOST_BLOCKLIST.get(source)
    if blocked_host:
        direct_url = clean_text(record.get("direct_url"))
        direct_key = external_url_key(direct_url, blocked_host)
        if direct_key:
            return direct_key
    return url_key(clean_text(record.get("source_url")))


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
        ensure_schema(self.conn)

    def close(self) -> None:
        self.conn.close()

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

    def sources_with_search_error(self, error: str) -> list[str]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT state.source
            FROM search_attempts AS attempt
            JOIN search_state AS state USING (search_key)
            WHERE attempt.run_id = ? AND attempt.error = ?
            ORDER BY state.source
            """,
            [self.run_id, error],
        ).fetchall()
        return [row[0] for row in rows]

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
        if offer.source == "cadremploi":
            record["source_id"] = cadremploi_offer_id(offer.source_url)
        observed_at = datetime.now(UTC)
        payload = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True)
        observation_id = sha256(payload.encode()).hexdigest()
        inserted = self.conn.execute(
            "INSERT OR IGNORE INTO offer_observations VALUES (?, ?, ?, ?::JSON) RETURNING observation_id",
            [observation_id, self.run_id, observed_at, payload],
        ).fetchone()
        if not inserted:
            observation = self.conn.execute(
                "SELECT observed_at FROM offer_observations WHERE observation_id = ?", [observation_id]
            ).fetchone()
            assert observation is not None
            observed_at = observation[0]
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
            archived_exclusions = source.execute("SELECT url_key FROM replay_excluded_offer_urls").fetchall()
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
            if archived_exclusions and not same_db:
                self.conn.executemany(
                    "INSERT OR IGNORE INTO replay_excluded_offer_urls VALUES (?)", archived_exclusions
                )
            excluded = {
                row[0] for row in self.conn.execute("SELECT url_key FROM replay_excluded_offer_urls").fetchall()
            }
            replayed = 0
            for observed_at, payload in rows:
                record = json.loads(payload)
                if excluded & offer_link_keys(record.get("source_url"), record.get("direct_url")):
                    continue
                self._upsert_record(record, observed_at)
                replayed += 1
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return replayed

    def _upsert_record(self, record: dict[str, object], observed_at: datetime) -> bool:
        record["date_posted"] = absolute_posted_date(
            record.get("date_posted"),
            now=_datetime_value(record.get("scraped_at")),
        )
        source_url_key = _offer_url_key(record)
        source = record["source"]
        identity = source_url_key or record["source_url"]
        existing = self.conn.execute(
            """
            SELECT offer_id FROM raw_offers
            WHERE source = ? AND (source_url_key = ? OR source_url = ?)
            ORDER BY (source_url = ?) DESC, (source_url_key = ?) DESC NULLS LAST, first_seen_at, offer_id
            LIMIT 1
            """,
            [source, identity, record["source_url"], record["source_url"], identity],
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
        params: list[object] = [
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
                    WHEN excluded.source = 'jobijoba'
                         AND json_type(excluded.raw_json, '$.json_ld.description') = 'VARCHAR'
                         AND excluded.description IS NOT NULL
                    THEN excluded.description
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
        for key in offer_link_keys(record.get("source_url"), record.get("direct_url")):
            self.conn.execute("DELETE FROM excluded_offer_urls WHERE url_key = ?", [key])
            self.conn.execute("DELETE FROM replay_excluded_offer_urls WHERE url_key = ?", [key])
        return True

    def canonical_dataframe(
        self, *, allows: Callable[[pd.Series], bool] | None = None, exclude_contracts: bool = False
    ) -> pd.DataFrame:
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
            return pd.DataFrame(columns=pd.Index(CANONICAL_COLUMNS))

        frame["date_posted"] = frame.apply(
            lambda row: (
                absolute_posted_date(row["date_posted"], now=row["first_seen_at"])
                if pd.notna(row["date_posted"])
                else None
            ),
            axis=1,
        )
        frame["direct_url"] = frame["direct_url"].where(frame["direct_url"].map(is_application_destination), None)
        frame["group_key"] = self._build_group_keys(frame)
        if exclude_contracts:
            rejected_groups = frame.loc[frame.apply(_has_excluded_contract, axis=1), "group_key"]
            frame = frame.loc[~frame["group_key"].isin(rejected_groups)].copy()
        status_alias_keys: dict[str, set[str]] = {}
        if allows is not None and not frame.empty:
            accepted = frame.apply(allows, axis=1)
            # Keep tracker identities when a filter switches the winning source.
            for _, row in frame.loc[~accepted].iterrows():
                status_alias_keys.setdefault(row["group_key"], set()).update(
                    offer_link_keys(row["source_url"], row["direct_url"])
                )
            frame = frame.loc[accepted].copy()
        frame["source_priority"] = (
            frame.loc[:, "source"].map(self.source_priority).fillna(self.default_source_priority).astype(int)
        )

        candidates = frame.loc[frame["title"].notna() & frame["company"].notna()].copy()
        if candidates.empty:
            return pd.DataFrame(columns=pd.Index(CANONICAL_COLUMNS))

        candidates = candidates.sort_values(
            by=["group_key", "source_priority", "completeness_score", "scraped_at"],
            ascending=[True, True, False, False],
            kind="stable",
        )
        best_rows = candidates.drop_duplicates(subset=["group_key"], keep="first").copy()
        best_rows = best_rows.drop(columns=["first_seen_at", "last_seen_at"], errors="ignore")

        links = frame.groupby("group_key", sort=False).apply(aggregate_group_links, include_groups=False).reset_index()
        result = best_rows.merge(links, on="group_key", how="inner")
        result["status_alias_keys"] = result["group_key"].map(
            lambda key: tuple(sorted(status_alias_keys.get(key) or ()))
        )
        result["description"] = result.pop("group_description")
        result["date_posted"] = result["date_posted"].where(
            result["date_posted"].notna() & (result["date_posted"].astype(str).str.strip() != ""),
            result["group_date_posted"],
        )
        posted_dates = pd.to_datetime(result["date_posted"], errors="coerce", utc=True).dt.as_unit("us")
        last_seen_dates = pd.to_datetime(result["last_seen_at"], errors="coerce", utc=True).dt.as_unit("us")
        result["sort_date"] = posted_dates.fillna(last_seen_dates)
        result = result.sort_values(by=["sort_date", "company", "title"], ascending=[False, True, True], kind="stable")

        result = result.rename(
            columns={
                "source": "best_source",
                "source_url": "best_source_url",
                "direct_url": "best_direct_url",
            }
        )
        return result.loc[:, CANONICAL_COLUMNS]

    def _build_group_keys(self, frame: pd.DataFrame) -> pd.Series:
        return build_group_keys(
            frame, self.dedupe_primary_fields, self.dedupe_fallback_fields, self.dedupe_description_fields
        )

    def raw_dataframe(self) -> pd.DataFrame:
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

    def purge_excluded_offers(self) -> int:
        frame = self.conn.execute(
            """
            SELECT offer_id, dedupe_key, description_key, source, source_id, source_url, direct_url,
                   location, title, company, employment_type, description
            FROM raw_offers ORDER BY offer_id
            """
        ).df()
        if frame.empty:
            return 0
        rejected = frame.apply(_has_excluded_contract, axis=1)
        # A sparse copy must not hide the contract disclosed by another board for
        # the same offer. Use the same identity groups as the canonical export.
        groups = self._build_group_keys(frame)
        rejected_groups = set(groups[rejected])
        offer_ids = frame.loc[groups.isin(rejected_groups), "offer_id"].tolist()
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def _delete_offer_ids(self, offer_ids: list[str]) -> None:
        rows = self.conn.execute(
            "SELECT source_url, direct_url FROM raw_offers WHERE offer_id IN (SELECT unnest(?))", [offer_ids]
        ).fetchall()
        keys = list({(key,) for source_url, direct_url in rows for key in offer_link_keys(source_url, direct_url)})
        if keys:
            self.conn.executemany("INSERT OR IGNORE INTO replay_excluded_offer_urls (url_key) VALUES (?)", keys)
        params = [(offer_id,) for offer_id in offer_ids]
        try:
            self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
        except duckdb.Error:
            self.conn.execute("DROP INDEX IF EXISTS raw_offers_url_idx")
            try:
                self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
            finally:
                self.conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS raw_offers_url_idx ON raw_offers(source, source_url_key)"
                )

    def excluded_url_keys(self) -> set[str]:
        """Prevent old tracker copies from restoring offers removed by a purge."""
        rows = self.conn.execute("""
            SELECT url_key FROM excluded_offer_urls
            UNION
            SELECT url_key FROM replay_excluded_offer_urls
        """).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def search_key(source: str, query: str, location: str) -> str:
        return stable_hash(source, query, location)


def _has_excluded_contract(row: pd.Series) -> bool:
    return has_excluded_contract_signal(row.get("title"), row.get("employment_type"), row.get("description"))
