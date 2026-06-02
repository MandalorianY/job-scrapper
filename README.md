# Example Job Scraper

A configurable Python job-search scraper for example roles in Auvergne-Rhône-Alpes, France. It gathers offers from job boards and free public web sources, stores incremental raw results in DuckDB, deduplicates them, filters out student contracts, and exports a localized Excel tracker plus CSV files.

## Key Features

- Multi-source scraping with JobSpy, Crawlee, `httpx`, and BeautifulSoup.
- Incremental DuckDB storage with per-source search state and recoverable source errors.
- Cross-source deduplication by normalized title and company.
- Title-focused include filtering for example-role roles.
- Contract filtering for stages, internships, apprenticeships, alternance, and contrats de professionnalisation.
- Localized Excel tracker with status dropdowns, applied checkboxes, clickable links, filters, tables, salary columns, and a raw-data sheet.
- CSV exports for both canonical deduped offers and raw source rows.
- Opt-in live smoke tests for real job sites.

## Table of Contents

- [Tech Stack](#tech-stack)
- [Prerequisites](#prerequisites)
- [Getting Started](#getting-started)
- [Usage](#usage)
- [Configuration](#configuration)
- [Outputs](#outputs)
- [Architecture](#architecture)
- [Data Model](#data-model)
- [Filtering and Deduplication](#filtering-and-deduplication)
- [Testing](#testing)
- [Public Repository Safety](#public-repository-safety)
- [Troubleshooting](#troubleshooting)
- [Contributing](#contributing)
- [License](#license)

## Tech Stack

| Area | Technology |
| --- | --- |
| Language | Python 3.13+ |
| Package manager | uv |
| CLI | Typer, exposed as `job-scrapper` |
| Console output | Rich |
| Storage | DuckDB |
| Supported job boards | JobSpy for Indeed, LinkedIn, Glassdoor, and Google Jobs |
| Custom crawling | Crawlee Python `BeautifulSoupCrawler` |
| HTTP and parsing | `httpx`, BeautifulSoup, lxml |
| Settings | Pydantic Settings with YAML and `.env` support |
| Exports | pandas and OpenPyXL |
| Tests | pytest, pytest-cov, Hypothesis, Ruff, taskipy |

## Prerequisites

- Python 3.13 or newer.
- `uv` installed locally.
- Network access to the configured job sites.
- Optional: Excel, LibreOffice, or another spreadsheet app to open `exports/jobs.xlsx`.

Install `uv` if needed:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Confirm the runtime:

```bash
python --version
uv --version
```

## Getting Started

### 1. Clone the Repository

```bash
git clone https://github.com/<owner>/<repo>.git
cd <repo>
```

### 2. Install Dependencies

```bash
uv sync
```

This creates the virtual environment and installs the application, development tools, and test dependencies from `pyproject.toml` and `uv.lock`.

### 3. Prepare Local Configuration

Secrets belong in `.env`:

```bash
cp .env.example .env
```

You can run the scraper without editing anything else. The packaged defaults target configured example roles in Auvergne-Rhône-Alpes and write runtime artifacts to `data/` and `exports/`.

For local non-secret overrides, create `src/scrapper/config/config.local.yaml` with only the keys you want to change:

```yaml
queries:
  - your real role

locations:
  - Lyon, France

enabled_sources:
  linkedin: false
```

Keep shared profile defaults in `src/scrapper/config/config.dev.yaml` or `src/scrapper/config/config.prod.yaml`. Use environment variables when you want a one-off runtime override.

### 4. Run a Small Smoke Scrape

```bash
uv run job-scrapper --results-wanted 5 --custom-pages 1
```

This limits each query/source enough to validate the pipeline without doing a long scrape.

### 5. Re-export Existing Data

After the first scrape creates `data/job_search.duckdb`, you can regenerate CSV and Excel files without hitting external sites:

```bash
uv run job-scrapper --skip-custom --skip-jobspy
```

## Usage

Run the full scraper with defaults:

```bash
uv run job-scrapper
```

Override search terms:

```bash
uv run job-scrapper \
  --query "responsable example role" \
  --query "example stakeholder relations"
```

Override the configured locations and scrape size:

```bash
uv run job-scrapper \
  --location "Auvergne-Rhône-Alpes, France" \
  --results-wanted 20 \
  --custom-pages 2
```

Run only JobSpy sources:

```bash
uv run job-scrapper --skip-custom
```

Run only custom/free sources:

```bash
uv run job-scrapper --skip-jobspy
```

Force custom/detail scrapers to revisit previously seen detail pages:

```bash
uv run job-scrapper --refresh-known
```

Write to custom paths:

```bash
uv run job-scrapper \
  --db data/custom.duckdb \
  --export-dir exports/custom
```

## Configuration

Configuration is loaded through Pydantic Settings from several sources. Higher entries override lower entries:

1. CLI/init overrides.
2. Environment variables.
3. `.env`.
4. `src/scrapper/config/config.local.yaml`.
5. Selected environment YAML, such as `src/scrapper/config/config.dev.yaml` or `src/scrapper/config/config.prod.yaml`.
6. Packaged defaults in `src/scrapper/config/config.yaml`.

Select the environment profile:

```bash
SCRAPPER_APP_ENV=prod uv run job-scrapper
```

Ignore the local YAML override for CI-style or template-only runs:

```bash
SCRAPPER_DISABLE_LOCAL_CONFIG=1 uv run job-scrapper --skip-custom --skip-jobspy
```

Nested environment variables use `__`:

```bash
SCRAPPER_LOCATIONS='["Auvergne-Rhône-Alpes, France"]' SCRAPPER_LOCATION_LABEL="Auvergne-Rhône-Alpes, France" uv run job-scrapper
SCRAPPER_PATHS__DATABASE_PATH="data/jobs.duckdb" uv run job-scrapper
SCRAPPER_WEB_SEARCH__QUERY_TEMPLATE='"{query}" emploi CDI "{location}" -stage -alternance' uv run job-scrapper
```

### Important Settings

| Setting | Purpose | Default |
| --- | --- | --- |
| `queries` | Search terms to run against every source | example role-oriented terms |
| `locations` | Target search locations used by compatible sources | `Auvergne-Rhône-Alpes, France` |
| `location_label` | Readable label for broad/regional searches and exports | `Auvergne-Rhône-Alpes, France` |
| `distance_km` | Radius passed to compatible boards | `50` |
| `include_terms` | Title terms required for broad sources | Example/sample/demo placeholder terms |
| `exclude_terms` | Student-contract terms to reject | Stage, alternance, apprentissage, etc. |
| `jobspy_sites` | JobSpy boards to run | Indeed, LinkedIn, Glassdoor, Google |
| `custom_sites` | Custom source groups | HelloWork, Welcome to the Jungle, web search, aggregators |
| `default_hours_old` | Initial JobSpy lookback window | `336` |
| `overlap_hours` | Incremental overlap after successful runs | `24` |
| `custom_max_pages` | Search/list pages per custom source | `2` |
| `custom_max_detail_pages` | Detail-page cap for custom crawling | `80` |
| `request_delay_secs` | Conservative pacing for custom crawlers | `1.5` |
| `paths.database_path` | DuckDB database location | `data/job_search.duckdb` |
| `paths.export_dir` | Export directory | `exports` |

### Secrets

The project should run with free/public defaults. If a source-specific key is needed locally, keep it in `.env` and never commit it:

```bash
SCRAPPER_WELCOME_TO_THE_JUNGLE__ALGOLIA_API_KEY=
SCRAPPER_APP_ENV=dev
```

`.env` is ignored by Git. The committed `.env.example` intentionally contains empty placeholders only.

## Outputs

Runtime outputs are ignored by Git:

| Path | Description |
| --- | --- |
| `data/job_search.duckdb` | DuckDB database with raw offers and incremental search state |
| `exports/jobs.xlsx` | localized Excel tracker |
| `exports/jobs.csv` | Deduped canonical offers |
| `exports/jobs_raw.csv` | Raw source rows |

The Excel workbook contains:

- `Offres`: the user-facing localized tracker.
- `Données brutes`: raw normalized rows from every source.

The first columns in `Offres` are designed for action:

- `Postulé`: formula-driven checkbox based on `Statut`.
- `Statut`: dropdown with values such as `À étudier`, `À postuler`, and `Postulé`.
- `Postuler`: best available apply link.
- `Offre source`: original source listing.
- `Autres liens`: additional direct/source links preserved from duplicate offers.

## Architecture

```text
.
├── main.py                         # Typer entry point when run as a module
├── pyproject.toml                  # Project metadata, dependencies, CLI script, test/lint config
├── src/
│   └── scrapper/
│       ├── cli.py                  # Orchestration: scrape, purge, export, summary
│       ├── config/
│       │   ├── settings.py         # Pydantic Settings models and source precedence
│       │   ├── config.yaml         # Packaged defaults
│       │   ├── config.dev.yaml     # Development profile overrides
│       │   └── config.prod.yaml    # Production/longer-run profile overrides
│       ├── export.py               # localized Excel and CSV generation
│       ├── models.py               # JobOffer normalized source contract
│       ├── storage.py              # DuckDB schema, incremental state, upsert, dedupe queries
│       ├── text.py                 # Cleanup, URL keys, stable hashes, include/exclude filters
│       └── scrapers/
│           ├── jobspy_sources.py   # Indeed, LinkedIn, Glassdoor, Google Jobs via JobSpy
│           ├── hellowork.py        # Crawlee BeautifulSoup crawler
│           ├── wttj.py             # Welcome to the Jungle public Algolia flow
│           ├── web_search.py       # Free public HTML search discovery
│           ├── aggregators.py      # localized aggregator pages and site-scoped discovery
│           └── common.py           # Shared scraper extraction helpers
└── tests/
    ├── integration/                # Opt-in live source smoke tests
    └── unit/                       # Text, storage, export, and scraper helper tests
```

### Run Flow

```text
CLI options
  -> SearchConfig
  -> JobStore opens DuckDB
  -> JobSpy sources, unless skipped
  -> custom/free sources, unless skipped
  -> purge excluded student contracts
  -> purge non-matching titles
  -> export Excel and CSV files
  -> print Rich summary table
```

Each source records search state:

1. `store.start_search(source, query, location)`
2. Scrape/list/detail work.
3. `store.finish_search(..., seen_count)` on success.
4. `store.finish_search(..., error=str(exc))` on recoverable failure.

This lets one failing board preserve its error in DuckDB without blocking exports from other sources.

### Source Families

| Source family | Module | Notes |
| --- | --- | --- |
| JobSpy boards | `scrapers/jobspy_sources.py` | Indeed, LinkedIn, Glassdoor, Google Jobs. Uses incremental `hours_old` with overlap. |
| HelloWork | `scrapers/hellowork.py` | Crawlee `BeautifulSoupCrawler` with LIST and DETAIL request labels. |
| Welcome to the Jungle | `scrapers/wttj.py` | Uses the public Algolia-backed frontend search flow or a locally configured key. |
| Web search | `scrapers/web_search.py` | Free public HTML search fallbacks and detail-page extraction. |
| Aggregators | `scrapers/aggregators.py` | Example Country Travail, Emploi Territorial, Meteojob, Jobijoba, jobs that makesense, Apec, Isarta, Emploi Collectivités, Cadremploi, Optioncarriere. |

## Data Model

Every scraper returns `JobOffer` objects before storage:

| Field | Description |
| --- | --- |
| `source` | Source name, such as `indeed` or `hellowork` |
| `source_url` | Original listing/detail URL |
| `direct_url` | Apply URL when different from the source page |
| `source_id` | Source-specific stable identifier when available |
| `title` | Job title |
| `company` | Hiring company or organization |
| `location` | Job location |
| `description` | Cleaned job description |
| `date_posted` | Source-provided publication date |
| `employment_type` | Contract type when available |
| `salary`, `min_amount`, `max_amount`, `currency`, `interval` | Salary details |
| `is_remote` | Remote-work signal |
| `company_url`, `company_description` | Company metadata |
| `emails` | Extracted contact emails when a source provides them |
| `search_query`, `search_location` | Query context |
| `raw` | Source payload or extraction metadata |

DuckDB stores:

- `raw_offers`: normalized raw offers, scores, hashes, timestamps, and raw JSON.
- `search_state`: per-source/per-query run history, last success time, seen count, and last error.

## Filtering and Deduplication

### Include Filtering

Broad sources can return unrelated roles for generic queries. The scraper therefore requires example-role terms in the title using `contains_any_terms((title,), config.include_terms)`.

### Student Contract Filtering

The scraper rejects offers when the title or contract type contains excluded terms, and it applies stricter description patterns for genuine contract signals:

- Stage/internship.
- Alternance.
- Apprenticeship/apprentissage.
- Contrat de professionnalisation.

It avoids rejecting a role only because the description mentions managing an intern, internal example role, or international work.

### Deduplication

Deduplication uses normalized title and company. Location is added only when title or company is missing. The canonical export chooses the row with:

1. Highest completeness score.
2. Most recent scrape timestamp.

Duplicate source and apply links are still preserved in the exported tracker.

## Testing

Run a compile check:

```bash
uv run python -m compileall src main.py
```

Run lint plus pytest with coverage:

```bash
uv run task test
```

Run faster pytest iteration without coverage:

```bash
uv run pytest -x -q --no-cov
```

Run the live website smoke test suite:

```bash
SCRAPPER_RUN_REAL_SMOKE=1 uv run pytest tests/integration/test_real_scrape_smoke.py --no-cov
```

Run one live source:

```bash
SCRAPPER_RUN_REAL_SMOKE=1 \
SCRAPPER_REAL_SMOKE_SOURCE=hellowork \
uv run pytest tests/integration/test_real_scrape_smoke.py --no-cov
```

External job sites may throttle, block, or change markup. Live source failures are expected occasionally; source-level errors are recorded in `search_state` and should not prevent export generation.

### Inspect the Workbook

```bash
uv run python - <<'PY'
from openpyxl import load_workbook

wb = load_workbook("exports/jobs.xlsx", data_only=False)
ws = wb["Offres"]

print(wb.sheetnames)
print(ws.max_row - 1, ws.max_column)
print(list(ws.tables.keys()))
print(ws.auto_filter.ref)
print(ws["A2"].value)
print(ws["C2"].value)
PY
```

Expected checks after export changes:

- `exports/jobs.xlsx` exists.
- `Offres` has rows when the database has matching offers.
- `Données brutes` exists.
- `TableOffres` exists when there is at least one exported row.
- `A2` contains the `Postulé` formula when there is at least one exported row.
- Apply/source link columns contain `HYPERLINK(...)` formulas when URLs exist.
- Salary min/max columns are annual numeric values or empty.

## Public Repository Safety

The repository is prepared so runtime and local-only files stay out of Git:

- `.env` for secrets.
- `src/scrapper/config/config.local.yaml` for local non-secret overrides.
- `data/` and `exports/` for generated scraper outputs.
- `.agents/` and `skills-lock.json` for local agent/skill state.
- `.DS_Store`, caches, coverage HTML, build artifacts, and virtual environments.

Before publishing, run:

```bash
git status --short
git check-ignore -v .env src/scrapper/config/config.local.yaml data/job_search.duckdb exports/jobs.xlsx .agents/ skills-lock.json
rg -n --hidden -g '!.git/**' -g '!uv.lock' -g '!data/**' -g '!exports/**' -g '!.venv/**' \
  -e 'api[_-]?key|token|secret|password|authorization|bearer|ABSOLUTE_HOME_PATH'
```

Only commit template/example configuration files. Do not commit real exports, DuckDB databases, local search results, private API keys, browser cookies, or generated caches.

## Troubleshooting

### `uv` Cannot Find Python 3.13

Install or let `uv` manage a compatible Python:

```bash
uv python install 3.13
uv sync
```

### Google Jobs Returns Zero Rows

Google Jobs is attempted through JobSpy. Some runs may record an upstream `initial cursor not found` / zero-row condition in DuckDB search state. This is non-fatal; the scraper continues with the other free sources.

### A Source Fails or Times Out

Source failures are caught at the source/query level. Run a smaller scrape while debugging:

```bash
uv run job-scrapper --results-wanted 1 --custom-pages 1
```

Then inspect `search_state` in DuckDB or rerun just the relevant source through the integration smoke test.

### The Excel File Has No Rows

Common causes:

- External sites returned no matching results.
- The include filter rejected titles outside the example-role/sample-role/demo-role scope.
- The exclude filter removed student contracts.
- The database path points to a new/empty DuckDB file.

Try a narrower query:

```bash
uv run job-scrapper --query "example coordinator" --location "Auvergne-Rhône-Alpes, France" --results-wanted 10 --custom-pages 1
```

### Salary Columns Are Empty

The exporter only fills annual numeric salary columns when it can confidently parse an annual or monthly range. Hourly, ambiguous, or raw schema payloads are intentionally left empty to avoid misleading values.

## Contributing

Small, source-specific changes are easiest to review:

1. Put new source parsing in `src/scrapper/scrapers/<source>.py`.
2. Put shared extraction helpers in `src/scrapper/scrapers/common.py`.
3. Put text/filter/dedupe changes in `src/scrapper/text.py`.
4. Put schema/query changes in `src/scrapper/storage.py`.
5. Add focused unit tests and, when useful, an opt-in live smoke test.

Run before opening a pull request:

```bash
uv run python -m compileall src main.py
uv run task test
uv run job-scrapper --skip-custom --skip-jobspy
```

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
