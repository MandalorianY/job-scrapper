from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import duckdb
import pandas as pd

from scrapper.models import JobOffer
from scrapper.text import (
    clean_text,
    completeness_score,
    contains_any_terms,
    description_dedupe_key_for_fields,
    has_excluded_contract_signal,
    dedupe_key_for_fields,
    stable_hash,
    url_key,
)


class JobStore:
    def __init__(
        self,
        path: Path,
        source_priority: Mapping[str, int] | None = None,
        default_source_priority: int = 100,
        dedupe_primary_fields: tuple[str, ...] = ("title", "company"),
        dedupe_fallback_fields: tuple[str, ...] = ("location",),
        dedupe_description_fields: tuple[str, ...] = ("description", "company", "location"),
    ) -> None:
        self.path = path
        self.source_priority = {str(source): int(priority) for source, priority in (source_priority or {}).items()}
        self.default_source_priority = default_source_priority
        self.dedupe_primary_fields = tuple(dedupe_primary_fields)
        self.dedupe_fallback_fields = tuple(dedupe_fallback_fields)
        self.dedupe_description_fields = tuple(dedupe_description_fields)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(path))
        self.ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def ensure_schema(self) -> None:
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
        self._ensure_optional_columns()

    def _ensure_optional_columns(self) -> None:
        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info('raw_offers')").fetchall()
        }
        if "description_key" not in columns:
            self.conn.execute("ALTER TABLE raw_offers ADD COLUMN description_key VARCHAR")

    def start_search(self, source: str, query: str, location: str) -> str:
        key = self.search_key(source, query, location)
        now = datetime.now(timezone.utc)
        self.conn.execute(
            """
            INSERT INTO search_state(search_key, source, query, location, last_started_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(search_key) DO UPDATE SET
                last_started_at = excluded.last_started_at,
                last_error = NULL
            """,
            [key, source, query, location, now],
        )
        return key

    def finish_search(self, source: str, query: str, location: str, seen_count: int, error: str | None = None) -> None:
        key = self.search_key(source, query, location)
        now = datetime.now(timezone.utc)
        if error:
            self.conn.execute(
                """
                UPDATE search_state
                SET last_error = ?, last_seen_count = ?
                WHERE search_key = ?
                """,
                [error, seen_count, key],
            )
            return
        self.conn.execute(
            """
            UPDATE search_state
            SET last_success_at = ?, last_seen_count = ?, last_error = NULL
            WHERE search_key = ?
            """,
            [now, seen_count, key],
        )

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
            "SELECT 1 FROM raw_offers WHERE source = ? AND source_url_key = ? LIMIT 1",
            [source, key],
        ).fetchone()
        return row is not None

    def upsert_offer(self, offer: JobOffer) -> bool:
        record = offer.to_record()
        source_url_key = url_key(record["source_url"])
        offer_id = stable_hash(record["source"], record.get("source_id"), source_url_key or record["source_url"])
        duplicate_key = dedupe_key_for_fields(
            record,
            fields=self.dedupe_primary_fields,
            fallback_fields=self.dedupe_fallback_fields,
        )
        duplicate_description_key = description_dedupe_key_for_fields(
            record,
            fields=self.dedupe_description_fields,
        )
        now = datetime.now(timezone.utc)
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
            now,
            now,
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
                date_posted = COALESCE(excluded.date_posted, raw_offers.date_posted),
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
            """,
            params,
        )
        return True

    def canonical_dataframe(self):
        frame = self.conn.execute(
            """
            SELECT
                offer_id,
                dedupe_key,
                description_key,
                source,
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
            """
        ).fetchdf()
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

        frame["description_key"] = frame.apply(
            lambda row: row["description_key"]
            or description_dedupe_key_for_fields(row, fields=self.dedupe_description_fields),
            axis=1,
        )
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
        result["date_posted"] = result["date_posted"].where(
            result["date_posted"].notna() & (result["date_posted"].astype(str).str.strip() != ""),
            result["group_date_posted"],
        )
        result["sort_date"] = result["date_posted"].fillna(result["last_seen_at"].astype(str))
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

    @staticmethod
    def _build_group_keys(frame: pd.DataFrame) -> pd.Series:
        parents = list(range(len(frame)))

        def find(index: int) -> int:
            while parents[index] != index:
                parents[index] = parents[parents[index]]
                index = parents[index]
            return index

        def union(left: int, right: int) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[right_root] = left_root

        seen_keys: dict[tuple[str, str], int] = {}
        for index, row in enumerate(frame.itertuples(index=False)):
            for kind, value in (("title", row.dedupe_key), ("description", row.description_key)):
                if not value:
                    continue
                key = (kind, value)
                previous = seen_keys.get(key)
                if previous is None:
                    seen_keys[key] = index
                    continue
                union(index, previous)

        return pd.Series((str(find(index)) for index in range(len(frame))), index=frame.index, dtype="string")

    @staticmethod
    def _aggregate_group_links(group: pd.DataFrame) -> pd.Series:
        return pd.Series(
            {
                "sources": ", ".join(sorted({value for value in group["source"].dropna() if value})),
                "all_source_links": "\n".join(sorted({value for value in group["source_url"].dropna() if value})),
                "all_direct_links": JobStore._join_optional_links(group["direct_url"]),
                "group_date_posted": JobStore._best_group_value(group, "date_posted"),
                "duplicate_count": int(len(group)),
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
        ).fetchdf()

    def purge_excluded_offers(self, exclude_terms: tuple[str, ...]) -> int:
        rows = self.conn.execute(
            "SELECT offer_id, title, employment_type, description FROM raw_offers"
        ).fetchall()
        offer_ids = [
            offer_id
            for offer_id, title, employment_type, description in rows
            if has_excluded_contract_signal(title, employment_type, description, exclude_terms)
        ]
        if not offer_ids:
            return 0
        self._delete_offer_ids(offer_ids)
        return len(offer_ids)

    def purge_nonmatching_offers(self, include_terms: tuple[str, ...]) -> int:
        rows = self.conn.execute(
            "SELECT offer_id, title FROM raw_offers"
        ).fetchall()
        offer_ids = [
            offer_id
            for offer_id, title in rows
            if not contains_any_terms((title,), include_terms)
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

    def _delete_offer_ids(self, offer_ids: list[str]) -> None:
        params = [(offer_id,) for offer_id in offer_ids]
        try:
            self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
        except duckdb.Error:
            self.conn.execute("DROP INDEX IF EXISTS raw_offers_url_idx")
            try:
                self.conn.executemany("DELETE FROM raw_offers WHERE offer_id = ?", params)
            finally:
                self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS raw_offers_url_idx ON raw_offers(source, source_url_key)")

    @staticmethod
    def search_key(source: str, query: str, location: str) -> str:
        return stable_hash(source, query, location)
