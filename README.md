# Job Scrapper

A job scraper for France that deduplicates listings, filters out student contracts, and preserves application statuses across Excel exports.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run from the checkout root. The project defaults to Python 3.14 and requires at least 3.13.

```sh
uv run --locked job-scrapper
```

## Docker CLI

With Docker running, start a live scrape from the checkout root.

```sh
./deploy/run-docker.sh
```

The launcher builds the image and uses the checkout’s local config, `data/`, and `exports/`.

To run `job-scrapper` from any directory, link the launcher into `~/.local/bin` if it’s on your `PATH`.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/deploy/run-docker.sh" "$HOME/.local/bin/job-scrapper"
```

Recreate the link if you move the checkout.

## Configuration

The bundled settings use sample roles in Auvergne-Rhône-Alpes. Set your roles and search area in `src/scrapper/config/config.local.yaml`.

```yaml
queries: [data analyst]
include_terms: [data analyst, analyste de données]
locations: ["Toulouse, France"]
location_label: "Toulouse, France"
distance_km: 10
location_scope:
  radius_km: 10
  centers:
    - name: Toulouse
      latitude: 43.6007
      longitude: 1.4328
```

Pass `--location` once per French town to update search routing for a run. Use `--show-config` to inspect settings and their origins. Keep credentials in `.env`. See the [example configuration](src/scrapper/config/config.yaml) and [.env.example](.env.example).

## Job sources

Use `--status` to see enabled sources. Set a source to `false` under `enabled_sources` in `config.local.yaml` to disable it.

✅ Working · ⚠️ Unverified · ⛔ Blocked or disabled

| Source | Config key | Status |
| --- | --- | --- |
| Indeed | `indeed` | ✅ |
| LinkedIn | `linkedin` | ✅ |
| Glassdoor | `glassdoor` | ✅ |
| Google Jobs | `google` | ⚠️ JobSpy reported `no job data returned`. |
| HelloWork | `hellowork` | ✅ |
| Welcome to the Jungle | `welcome_to_the_jungle` | ✅ |
| France Travail | `france_travail` | ✅ |
| Emploi Territorial | `emploi_territorial` | ✅ |
| Meteojob | `meteojob` | ✅ |
| Jobijoba | `jobijoba` | ✅ |
| Jobs that makesense | `makesense` | ✅ |
| Apec | `apec` | ⛔ Disabled by default after a DataDome challenge returned HTTP 403. |
| Isarta | `isarta` | ✅ |
| Emploi-Collectivités | `emploi_collectivites` | ✅ |
| Cadremploi | `cadremploi` | ✅ |
| Optioncarriere | `optioncarriere` | ⛔ A verification challenge blocked discovery. |

## Results

| File | Contents |
| --- | --- |
| `exports/jobs.xlsx` | French Excel tracker |
| `exports/jobs.csv` | Deduplicated listings |
| `exports/jobs_raw.csv` | Source listings |
| `data/job_search.duckdb` | Listings and search history |
| `data/job-scrapper.jsonl` | Run logs |

Use `--help` for available options or `--export-only` to regenerate files from DuckDB without scraping.

## Replay and recovery

Rebuild from up to 90 days of saved observations using the current filters.

```sh
uv run job-scrapper --replay-from data/job_search.duckdb --db data/rebuilt.duckdb --export-dir exports/rebuilt
```

Keep the previous workbook to carry application statuses into a rebuilt tracker. Query DuckDB’s `source_health` view to diagnose source failures.

## Development

```sh
uv run --locked --group test task test
```

Set `SCRAPPER_RUN_REAL_SMOKE=1` to include live smoke tests.

[Linux scheduling](deploy/README.md) · [MIT License](LICENSE)
