# Replay and recovery

## Rebuild from saved observations

Replay applies the current filters to up to 90 days of saved observations without
scraping. Run from the checkout root and choose a separate database and export
folder for the rebuild.
Replay can recover archived offers that now match a wider title, company, or
location filter. The archive contains offers that reached normalization within
the original search limits. Earlier versions archived only accepted offers.
Replay saves the resolved geographic scope for later exports from the rebuilt
database. Contract exclusions carry over and apply to rows already in that
database. A fresh, accepted scrape of the same source listing can supersede
earlier contract evidence.
Replay reads separate archives in read-only mode. Use an archive created or
opened for a normal run by the current version.

To carry application statuses forward, put a copy of your editable tracker in
`exports/rebuilt` before running the command. Copy `.jobsfr-tracker-history.csv`
too if it exists. It holds statuses for rows temporarily hidden by filters.

If JobsFR reports that it could not read the primary tracker, the original
file is still in place. Move it to a backup location before regenerating from
DuckDB, or restore a healthy copy first to carry application statuses forward.

```sh
uv run --locked jobsfr replay \
  --archive data/job_search.duckdb \
  --db data/rebuilt.duckdb \
  --export-dir exports/rebuilt \
  --log-file data/rebuilt.jsonl
```

Use `--profile PATH` if you normally run with a profile. Add `--extra ods` before
`jobsfr` if ODS is selected. Keep a full copy of the original DuckDB file to
retain listings outside the observation archive.

## Diagnose source failures

Open the original database with the DuckDB CLI in read-only mode.

```sh
duckdb -readonly data/job_search.duckdb
```

The `source_health` view shows the latest recorded attempt for each source, query,
and location alongside the last successful run. When JobSpy reaches its result
limit, Welcome to the Jungle reports pages beyond its configured limit, or a
direct aggregator leaves discovered detail links unchecked, the attempt keeps
the previous watermark. Use the original database for this query because
replay imports observations and starts with empty search history.

```sql
SELECT source, query, location, status, error, last_success_at
FROM source_health
ORDER BY source, query, location;
```
