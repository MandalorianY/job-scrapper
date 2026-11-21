# Job Scrapper

A job scraper for France that deduplicates listings, filters out student contracts, and preserves application statuses across Excel exports.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run from the checkout root. The project defaults to Python 3.14 and requires at least 3.13.

```sh
uv run --locked job-scrapper
```

Download a build for your platform from [GitHub Releases](https://github.com/MandalorianY/job-scrapper/releases).

### Docker CLI

With Docker running, run `./deploy/run-docker.sh` from the checkout root.
See [Docker setup](deploy/README.md#docker-cli) for host paths and launcher setup.

## Homebrew

Run `job-scrapper` from the directory where you want to keep `data/` and `exports/`.

Install the latest published release from the project's tap.

```sh
brew install mandaloriany/tap/job-scrapper
```

## Job sources

Use `--status` for an offline summary of search settings, enabled sources, and
tracker sync. Set a source to `false` under `enabled_sources` in
`config.local.yaml` to disable it.

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

## Configuration

Save your search settings in `config.local.yaml`.

- **Checkout and Docker** use `src/scrapper/config/config.local.yaml`.
- **Homebrew and standalone builds** use `config.local.yaml` in the directory where you run the scraper.

```yaml
queries: [data analyst]
include_terms: [data analyst, analyste de données]
locations: ["Nantes, France"]
distance_km: 10
```

Use `--profile` to select a separate YAML file. Preview the bundled Toulouse Graphiste profile with this command.

```sh
uv run --locked job-scrapper --profile src/scrapper/config/toulouse-graphiste.yaml --status
```

See the [configuration reference](docs/configuration.md) for geography, aggregator overrides, and file paths.

## Results

| File | Contents |
| --- | --- |
| `exports/jobs.xlsx` | French Excel tracker |
| `exports/jobs.ods` | French LibreOffice tracker |
| `exports/jobs.csv` | Deduplicated listings |
| `exports/jobs_raw.csv` | Source listings |
| `data/job_search.duckdb` | Listings and search history |
| `data/job-scrapper.jsonl` | Run logs |

Use `--help` for available options or `--export-only` to regenerate files from an existing DuckDB without scraping.

### Export formats

Choose one or more formats in your local configuration or profile. The default
is `[xlsx, csv]`. Selecting `csv` writes both CSV files.

```yaml
export:
  formats: [ods, xlsx, csv]
```

Edit the first format in the list to carry application statuses forward. If that
file is missing, the exporter looks for another selected workbook, then an
existing XLSX or CSV tracker.

Select `[ods]` for just the LibreOffice tracker. Whenever ODS is selected, run
with the optional dependency.

```sh
uv run --locked --extra ods job-scrapper --export-only
```

The ODS tracker uses the same `export.excel_columns` list. Include `Statut` for the
dropdown and `Rejeté` for a native checkbox. `Postulé` follows `Statut` when both
columns are present and is a checkbox when configured alone.

### Tracker sync

Use rclone to sync the editable tracker with a private remote.
See the [tracker sync guide](docs/tracker-sync.md).

## Replay and recovery

See the [recovery guide](docs/recovery.md) to rebuild from saved observations or diagnose a source failure.

## Development

Run the offline checks before submitting changes.

```sh
uv run --locked --group test --extra ods poe check
```

See the [contributor commands](AGENTS.md#useful-commands) for pre-push hook setup and the development workflow.

[Scheduled runs](deploy/README.md#scheduled-runs) · [MIT License](LICENSE)
