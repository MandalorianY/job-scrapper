"""Split a deduplicated vacancy into separately observed publications for export."""

from __future__ import annotations

from bisect import bisect_right
from datetime import date, timedelta
from typing import cast

import pandas as pd

from jobsfr.offers.dates import parse_posted_date
from jobsfr.offers.text import posting_url_key

_REPOST_GAP = timedelta(days=7)
_PUBLICATION_DATE_TOLERANCE = timedelta(days=2)


def _published_day(value: object) -> date | None:
    parsed = parse_posted_date(value) if isinstance(value, str) else None
    return parsed.day if parsed else None


def split_reposts(frame: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """Return per-publication group keys and dates for confirmed reposts."""
    group_keys = cast("pd.Series", frame["group_key"]).copy()
    reposted_on = pd.Series(index=frame.index, dtype="string")
    repeated = frame.loc[frame["group_key"].duplicated(keep=False)].copy()
    repeated["published"] = repeated["date_posted"].map(_published_day)
    dated = repeated.dropna(subset=["published"]).sort_values("published", kind="stable")

    for group, offers in dated.groupby("group_key", sort=False):
        dates = _repost_dates(offers)
        if not dates:
            continue
        periods: dict[tuple[str, str, str], int] = {}
        for index, source, url, source_id, published in offers[
            ["source", "source_url", "source_id", "published"]
        ].itertuples(index=True, name=None):
            # Boards can syndicate the new publication a day or two before its repeat listing is dated.
            period = bisect_right(dates, published + _PUBLICATION_DATE_TOLERANCE) - 1
            keys = _listing_keys(source, url, source_id)
            for key in keys:
                period = min(period, periods.get(key, period))
            periods.update(dict.fromkeys(keys, period))
            if period >= 0:
                repost_date = dates[period].isoformat()
                group_keys.at[index] = f"{group}:{repost_date}"
                reposted_on.at[index] = repost_date
    return group_keys, reposted_on


def _listing_keys(source: str, url: str, source_id: object) -> set[tuple[str, str, str]]:
    if not (link := posting_url_key(url)):
        return set()
    keys = {(source, "url", link)}
    if isinstance(source_id, str) and source_id:
        keys.add((source, "id", source_id))
    return keys


def _repost_dates(offers: pd.DataFrame) -> list[date]:
    first_by_board: dict[str, date] = {}
    seen: set[tuple[str, str, str]] = set()
    dates: list[date] = []
    for source, url, source_id, published in offers[["source", "source_url", "source_id", "published"]].itertuples(
        index=False, name=None
    ):
        if not (keys := _listing_keys(source, url, source_id)):
            continue
        if seen.intersection(keys):
            seen.update(keys)
            continue
        seen.update(keys)
        first = first_by_board.setdefault(source, published)
        if published - first >= _REPOST_GAP and (not dates or published - dates[-1] >= _REPOST_GAP):
            dates.append(published)
    return dates
