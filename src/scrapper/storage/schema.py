from __future__ import annotations

import duckdb


def ensure_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS excluded_offer_urls (url_key VARCHAR PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS replay_excluded_offer_urls (url_key VARCHAR PRIMARY KEY)
    """)
    conn.execute(
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
    conn.execute(
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
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS raw_offers_url_idx ON raw_offers(source, source_url_key)")
    conn.execute(
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
