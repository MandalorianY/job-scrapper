from __future__ import annotations

import re
from typing import Protocol, cast
from urllib.parse import urlparse

import pandas as pd

from scrapper.text import (
    GENERIC_LISTING_PATHS,
    clean_text,
    description_dedupe_key_for_fields,
    is_application_destination,
    listing_url_key,
    normalize_text,
)


class _OfferRow(Protocol):
    source: str
    source_url: str
    source_id: str | None
    direct_url: str | None
    location: str | None
    description_key: str | None
    dedupe_key: str | None

    def _asdict(self) -> dict[str, object]: ...


def _location_key(value: object | None) -> str:
    city = (clean_text(value) or "").split(",", 1)[0]
    return normalize_text(re.sub(r"\s*\(\d{2,5}\)$", "", city))


def build_group_keys(
    frame: pd.DataFrame,
    dedupe_primary_fields: tuple[str, ...],
    dedupe_fallback_fields: tuple[str, ...],
    dedupe_description_fields: tuple[str, ...],
) -> pd.Series:
    parents = list(range(len(frame)))
    rows = cast("list[_OfferRow]", list(frame.itertuples(index=False)))
    sources = [{row.source} for row in rows]
    locations = [{key} if (key := _location_key(row.location)) else set() for row in rows]
    apply_urls = [row.direct_url if is_application_destination(row.direct_url) else None for row in rows]
    destinations = [
        {(urlparse(url).hostname, key)} if url and (key := listing_url_key(url)) else set() for url in apply_urls
    ]
    use_location = any(
        "location" in fields for fields in (dedupe_primary_fields, dedupe_fallback_fields, dedupe_description_fields)
    )

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        parents[right] = left
        sources[left].update(sources[right])
        locations[left].update(locations[right])
        destinations[left].update(destinations[right])

    seen_identity: dict[tuple[str, ...], int] = {}
    for index, row in enumerate(rows):
        keys: list[tuple[str, ...]] = [
            ("url", key) for link in (row.source_url, apply_urls[index]) if (key := listing_url_key(link))
        ]
        source_id = row.source_id
        if (
            isinstance(source_id, str)
            and source_id
            and "/" not in source_id
            and source_id.lower() not in GENERIC_LISTING_PATHS
        ):
            host = (urlparse(row.source_url).hostname or "").removeprefix("www.")
            keys.append(("id", row.source, host, source_id))
        for key in keys:
            left, right = find(index), find(seen_identity.setdefault(key, index))
            if left != right:
                union(left, right)

    candidates: dict[tuple[str, str], set[int]] = {}
    for index, row in enumerate(rows):
        description_key = row.description_key or description_dedupe_key_for_fields(
            row._asdict(),
            fields=dedupe_description_fields,
        )
        fields = row._asdict()
        repost_key = None
        if dedupe_description_fields and all(fields.get(k) for k in ("title", "company", "location")):
            repost_key = description_dedupe_key_for_fields(
                fields, fields=("description", "title", "company", "location")
            )
        for kind, value in (("repost", repost_key), ("title", row.dedupe_key), ("description", description_key)):
            if not value:
                continue
            bucket = candidates.setdefault((kind, value), set())
            for previous in sorted(bucket):
                left, right = find(index), find(previous)
                if left == right or (kind != "repost" and sources[left] & sources[right]):
                    continue
                if any(
                    left_host == right_host and left_key != right_key
                    for left_host, left_key in destinations[left]
                    for right_host, right_key in destinations[right]
                ):
                    continue
                if (
                    use_location
                    and locations[left]
                    and locations[right]
                    and locations[left].isdisjoint(locations[right])
                ):
                    continue
                union(left, right)
            bucket.add(find(index))

    return pd.Series((str(find(index)) for index in range(len(frame))), index=frame.index, dtype="string")


def aggregate_group_links(group: pd.DataFrame) -> pd.Series:
    return pd.Series(
        {
            "sources": ", ".join(sorted({value for value in group["source"].dropna() if value})),
            "all_source_links": "\n".join(sorted({value for value in group["source_url"].dropna() if value})),
            "all_direct_links": _join_optional_links(group.loc[:, "direct_url"]),
            "group_description": max(group["description"].dropna(), key=len, default=None),
            "group_date_posted": _best_group_value(group, "date_posted"),
            "duplicate_count": len(group),
            "first_seen_at": group["first_seen_at"].min(),
            "last_seen_at": group["last_seen_at"].max(),
        }
    )


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


def _join_optional_links(values: pd.Series) -> str | None:
    deduped = sorted({value for value in values.dropna() if value})
    if not deduped:
        return None
    return "\n".join(deduped)
