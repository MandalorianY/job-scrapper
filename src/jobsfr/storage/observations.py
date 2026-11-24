"""Persist and read normalized observations for recovery and contract checks."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256

import duckdb

from jobsfr.offers.text import clean_text, provider_listing_key

OBSERVATION_RETENTION_DAYS = 90


def observation_timestamp(value: object | None) -> datetime | None:
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


def append_observation(conn: duckdb.DuckDBPyConnection, run_id: str, record: dict[str, object]) -> datetime:
    payload = json.dumps(record, ensure_ascii=False, default=str, sort_keys=True)
    observation_id = sha256(payload.encode()).hexdigest()
    observed_at = datetime.now(UTC)
    inserted = conn.execute(
        "INSERT OR IGNORE INTO offer_observations VALUES (?, ?, ?, ?::JSON) RETURNING observation_id",
        [observation_id, run_id, observed_at, payload],
    ).fetchone()
    if inserted:
        return observed_at
    observation = conn.execute(
        "SELECT observed_at FROM offer_observations WHERE observation_id = ?", [observation_id]
    ).fetchone()
    assert observation is not None
    return observation[0]


def excluded_url_keys(conn: duckdb.DuckDBPyConnection) -> set[str]:
    rows = conn.execute("""
        SELECT url_key FROM excluded_offer_urls UNION SELECT url_key FROM replay_excluded_offer_urls
    """).fetchall()
    return {key for (key,) in rows}


def contract_observations(conn: duckdb.DuckDBPyConnection) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT payload->>'source', payload->>'source_id', payload->>'source_url',
               CASE WHEN payload->>'_rejection' = 'contract' THEN payload END
        FROM offer_observations
        WHERE observed_at >= now() - INTERVAL (?) DAY
        QUALIFY row_number() OVER (
            PARTITION BY payload->>'source', coalesce(payload->>'_identity', payload->>'source_url')
            ORDER BY observed_at DESC, observation_id DESC
        ) = 1
        ORDER BY observed_at DESC, observation_id DESC
        """,
        [OBSERVATION_RETENTION_DAYS],
    ).fetchall()
    evidence = []
    seen: set[tuple[str, str, str]] = set()
    for source, source_id, source_url, payload in rows:
        if source_url and (provider_key := provider_listing_key(source_url, source_id)):
            identity = (source, *provider_key)
            if identity in seen:
                continue
            seen.add(identity)
        if payload is not None:
            evidence.append(json.loads(payload))
    return evidence


def rejection_counts(conn: duckdb.DuckDBPyConnection, run_id: str) -> list[dict[str, object]]:
    rows = conn.execute(
        """
        SELECT payload->>'source' AS source, payload->>'_rejection' AS reason, count(*)
        FROM offer_observations
        WHERE run_id = ? AND reason IS NOT NULL
        GROUP BY source, reason ORDER BY source, reason
        """,
        [run_id],
    ).fetchall()
    return [{"source": source, "reason": reason, "count": count} for source, reason, count in rows]
