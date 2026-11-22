# AGENTS.md

## Project Purpose

This repository is a Python job-search scraper for configured example roles in Example Region, Example Country. It stores incremental raw offers in DuckDB, dedupes offers across sources, filters out internships/apprenticeships/alternance, and exports a localized Excel tracker plus CSV files.

Primary outputs are ignored runtime artifacts:

- `data/job_search.duckdb`
- `data/job-scrapper.jsonl`
- `exports/jobs.xlsx` and/or `exports/jobs.ods` as selected in `export.formats`
- `exports/jobs.csv`
- `exports/jobs_raw.csv`

Do not commit generated data or exports unless the user explicitly asks for that.

## Stack

- Python 3.14 with `uv` by default, with Python 3.13 as the minimum supported version
- Typer CLI exposed as `job-scrapper`
- DuckDB for local storage and incremental search state
- JobSpy for supported boards: Indeed, LinkedIn, Glassdoor, Google Jobs, HelloWork
- `httpx` + BeautifulSoup for APIs, aggregator pages, and sitemaps
- OpenPyXL and pandas for localized Excel/CSV exports
- Pandera for validating the community employer CSV catalog
- PyInstaller in the locked `release` group for native Windows, Linux, and macOS releases. The release version lives in `pyproject.toml`.

The tracked `src/scrapper/config/config.yaml` is the public example template. Use `src/scrapper/config/config.local.yaml` (when present) for live repository settings such as query/location tuning and source-specific overrides. Use `--profile PATH` to select an alternate YAML with its own database, export, and log paths.
Installed builds also look for `config.local.yaml` in the working directory. Use paths under `data/` and `exports/` in Docker YAML profiles. The Docker launcher mounts host files passed via `--profile` or `--replay-from` and host destinations passed via `--db`, `--export-dir`, or `--log-file`.

## README Maintenance

Keep the root `README.md` focused on the first successful run. A new user
should be able to understand what the scraper does, install it, configure a
search, run it, and find the tracker. Keep one minimal search YAML example,
one `--profile` example, and one contributor check command.

- Update existing text in place when the first-run workflow or a documented
  fact changes, such as a supported source or default export. Add a new
  section only when first use requires it or the user explicitly requests it.
  Apply the same scope when using a README skill or template.
- Extend the existing guides in `docs/` for advanced configuration, tracker
  sync, replay, and troubleshooting. Use `deploy/README.md` for Docker path
  rules and scheduling, and the Useful Commands section below for contributor
  setup and checks. Use `job-scrapper --help` for the CLI option list and
  `.github/workflows/ci.yml` for the CI matrix. Link to the relevant guide
  from the README when readers need it.
- Review the README diff for growth. Move detail that a first-time user can
  defer to its guide, remove duplicate examples, and verify any changed
  commands and relative links.

## Useful Commands

Install/sync dependencies:

```bash
uv sync --locked --group test
```

Run the full scraper and export:

```bash
uv run job-scrapper
```

Run the full scraper and export with Docker:

```bash
./deploy/run-docker.sh
```

Install or update the schedule using `./deploy/install-timer.sh` on Linux or
`job-scrapper-install-timer` from the Homebrew installation on macOS. Both
commands accept `--profile PATH` and use the YAML `schedule.days` and
`schedule.time` settings.

Re-export from the existing DuckDB without scraping:

```bash
uv run job-scrapper --export-only
```

Run a smaller smoke scrape:

```bash
uv run job-scrapper --results-wanted 5 --custom-pages 1 
```

Compile check:

```bash
uv run python -m compileall src main.py
```

Run lint, formatting, and pytest with coverage:

```bash
uv run --locked poe test
```

Run the same offline checks as the pre-push hook, including ODS tests and
Pyright for Windows, Linux, and macOS.

```bash
uv run --locked --group test --extra ods poe check
```

Enable the hook for this clone with `git config --local core.hooksPath .githooks`.

Build native release assets for the current machine and smoke-test the installed CLI with `deploy/smoke_release.py`.

```bash
uv sync --locked --no-default-groups --group release --no-editable
uv run --locked --no-sync python deploy/build_release.py
```

Fast pytest iteration without coverage:

```bash
uv run --locked --group test pytest -x -q --no-cov
```

Inspect the current workbook:

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

## Architecture

- `src/scrapper/config/`: Pydantic Settings models, packaged `config.yaml` defaults, and `config.prod.yaml` scrape limits. `aggregator_specs.py` defines the built-in provider specs. Secrets belong in `.env`.
- `datasets/employers/v1/`: community-maintained employer catalog, bundled as a snapshot at build time. `company_types.py` loads and indexes it, `schemas/company_catalog.py` validates it, and `catalog_updates.py` refreshes the runtime cache from the default branch.
- `src/scrapper/models.py`: `JobOffer`, the single normalized offer shape used by every source.
- `src/scrapper/storage/`: DuckDB access in `store.py`, schema setup in `schema.py`, and canonical grouping in `dedupe.py`.
- `src/scrapper/text.py`: text cleanup, URL keys, stable hashes, dedupe key, include/exclude matching, completeness scoring.
- `src/scrapper/export/`: export orchestration, shared tracker state and salary normalization, and separate XLSX and ODS workbook writers.
- `src/scrapper/cli.py`: command-line options and run orchestration. `cli_output.py` renders the status screens and export summary.
- `src/scrapper/search_regions.py`: caches French municipality and region data alongside DuckDB so runs fetch only missing locations. Resolves provider routes and saves the radius filter for offline export and replay.
- `src/scrapper/scrapers/`: modules and packages grouped by scraper family. `aggregators/` separates discovery, detail parsing, and orchestration. `jobspy_sources/` separates search orchestration, offer normalization, and LinkedIn adaptation.

Keep source-specific parsing inside its scraper family. Put shared extraction helpers in `scrapers/common.py`, general text and matching helpers in `text.py`, and persistent schema and queries in `storage/`.

## Source Rules

Use `JobOffer` as the contract between scrapers and storage. Every scraper should populate as many fields as it can, especially:

- `source`
- `source_url`
- `direct_url` when the apply link differs from the listing page
- `title`
- `company`
- `location`
- `description`
- `date_posted`
- `employment_type`
- salary fields when available
- `search_query`
- `search_location`
- `raw`

Always store the source/original page. If an offer exposes a separate apply URL, preserve it in `direct_url`. The export uses the best apply link first while also preserving all source/direct links for duplicates.

## Filtering Rules

The scraper targets example-role jobs, not internships, stages, alternance, apprenticeships, contrats de professionnalisation, or derivative student contracts.

Use the existing helpers instead of ad hoc substring checks:

- `has_excluded_contract_signal(title, employment_type, description, config.exclude_terms)`
- `contains_any_terms((title,), config.include_terms)`

Use `config.title_is_allowed(title)` in scrapers to apply both include terms and
`excluded_title_patterns`. These patterns match normalized titles (lowercase,
without accents or punctuation); real-search exclusions belong in `config.local.yaml`.
Use `offer_matches_search` on normalized offers before upsert for title, contract,
company, and location validation. Offers need a description or a contract type.
Contract cleanup applies to the whole canonical duplicate group, so a sparse copy
cannot hide internship evidence from another source. Purged URL keys are retained
in `excluded_offer_urls` to prevent historical tracker copies from restoring them.
Geographic scope controls discovery and export. Keep existing raw listings when
their stored location falls outside the current profile.

Never hardcode a town name, town alias, or coordinates in scraper or filtering
logic. Use the same rules for every municipality with official geography data,
cached coordinates, and generic provider-format parsing. Put verified alternate
names in `location_scope.city_aliases` in the relevant profile. Use Toulouse
for public search examples and keep real-search towns and aliases in local
configuration. Test geographic changes across multiple towns.

Be careful with false positives. Do not remove an offer just because a description mentions managing an intern/alternant, internal example role, or international work. The stricter contract-stage logic lives in `text.py`; extend the phrase patterns there with focused examples when needed.

For broad sources, keep the include filter title-focused unless there is an explicit product decision to broaden it. This prevents generic "responsable" or non-configured example roles from leaking into the workbook.

## Dedupe And Completeness

Deduping matches provider IDs and listing-specific URLs first. Title/company and description matches across boards also consider location with the default settings. Same-board reposts require a matching title, employer, location, and substantial description. The canonical export ranks source priority, completeness, and scrape time, and preserves duplicate counts and links.

When changing dedupe behavior:

- Keep stable hashes deterministic.
- Avoid query parameters in URL keys except known job identifiers.
- Preserve `first_seen_at` and update `last_seen_at`.
- Verify that duplicate offers across boards keep the richest description and all links.

## Incremental Runs

All sources should call:

- `store.start_search(source, query, location)` before work.
- `store.finish_search(source, query, location, seen_count)` on success.
- `store.finish_search(..., error=str(exc))` on recoverable failure.

Scrapers should continue when one board fails. A single source outage should not block the Excel export.
Custom scrapers stop searching a source for the current run when they detect a
verification challenge. Record the failed search and include the source in the
CLI summary. The next run can try the source again. Timeouts, connection
failures, and retryable HTTP errors get one retry. Honor the host's Retry-After
window on HTTP 429 and 503. If the delay exceeds the wait budget, defer requests
to that host until the window expires and keep processing other sources.

Search attempts keep their run ID, outcome, and seen count in DuckDB. `source_health` shows the latest attempt and successful watermark. The 90-day observation archive records offers accepted at ingestion and supports offline `--replay-from`. Back up the full DuckDB file to retain listings that predate the archive or its retention window.

Custom/detail scrapers should skip known detail pages by default using `store.source_url_seen(...)`, unless `config.refresh_known_details` is true. Search/list pages may still be revisited to discover new links.

## JobSpy

Use JobSpy for Indeed, LinkedIn, Glassdoor, Google Jobs, and HelloWork. Use `enabled_sources` to disable individual boards or custom source groups.

Google is intentionally attempted through JobSpy first. Direct JobSpy Google Jobs may still return 0 results with `initial cursor not found`; when that happens, preserve the search-state error and continue with the other free sources. Do not invoke a paid Google Jobs fallback from the CLI unless paid API usage is explicitly requested.

For JobSpy changes:

- Keep `hours_old` incremental with the configured overlap.
- Set JobSpy's `fetch_description` for LinkedIn on a per-board copy of the search input so other boards in a batch keep their normal request count.
- Run HelloWork separately with detail fetching enabled and convert the configured search radius to whole miles for JobSpy.
- Keep Google-specific query construction in `_google_search_term`.
- Normalize all row values before building `JobOffer`.
- Filter before upsert.

## Google Jobs

Google Jobs uses JobSpy first. The CLI should stay free by default and should not call paid search APIs. Never hardcode keys.

## Aggregators And Custom Sources

Supported aggregators discover offers through their own APIs, search pages, or sitemaps. Treat missing direct routes and source failures as non-fatal and record their errors in `search_state`.

When adding aggregator discovery:

- Exclude the main boards already scraped directly.
- Reject informational pages such as job guides, salary pages, training pages, and job-description articles.
- Require a regional signal for Example Region.
- Require an example-role title.

Add each new direct source as a small, testable spec or module. Keep source-specific heuristics local. Reuse shared helpers for JSON-LD (`json_ld_jobposting`), company names, locations, metadata, salary text, and descriptions.

## Excel Export

The workbook must stay localized-first and easy to scan. The main sheet is `Offres`; raw source rows go in `Données brutes`.

Preserve these user-facing features:

- First columns are actionable: applied checkbox, status dropdown, clickable apply/source links.
- When both columns are configured, `Postulé` is formula-driven from `Statut`. With only `Postulé` configured, it is a manual checkbox.
- Filters, frozen panes, Excel table, conditional status colors.
- Annual salary min/max columns should be numeric when a reliable range exists.
- Ugly raw schema salary payloads should not appear in the main `Salaire` column.
- Empty salary is better than misleading salary.

Only annual amounts and monthly amounts multiplied by 12 belong in annual salary
columns. Hourly/daily rates, unknown units, foreign currencies, and invalid ranges
stay empty. The optional `À vérifier` column flags unusually low reported pay for
manual contract review; low pay alone must not exclude an offer.

The `job-scrapper` CLI writes local exports for normal runs, audits, and smoke runs.

Generate all files selected by `export.formats` in staging before replacing the
published files. The first selected format is the tracker to edit.

After export changes, regenerate and inspect `exports/jobs.xlsx` with OpenPyXL.
For ODS changes, run with `--extra ods` and check that native Calc checkbox and
status edits survive regeneration.

## Coding Style

- Pass file paths to `wc -l` to check file lengths, for example `wc -l src/scrapper/cli.py`. Keep maintained files at or below 600 lines by grouping related responsibilities into modules and packages. Allow rare exceptions for cohesive test files and keep generated files in their tool's format.
- Keep code small, readable, and source-specific.
- Annotate function parameters and return types, including private helpers and tests. Ruff enforces the annotations.
- Prefer reusable helpers over repeated parsing snippets.
- Prefer structured data such as JSON-LD, API JSON, and known IDs before scraping loose visible text.
- Use standard library parsing utilities for URLs, dates, and JSON rather than fragile string slicing when practical.
- Catch source-level scraper exceptions so other sources and export still run.
- Do not add new production dependencies unless they materially simplify the scraper.
- Do not store secrets in code, `.env`, DuckDB, exports, or logs.

## Cross-Platform Compatibility

Keep the native `job-scrapper` CLI, configuration, DuckDB storage, and exports
working on Windows, Linux, and macOS. Target Python 3.13 as the minimum and
preserve x64 and ARM64 support when changing dependencies or packaging.

- Use `pathlib.Path` for filesystem paths and `tempfile` for temporary files.
  Preserve paths with spaces or non-ASCII characters, specify the encoding
  for text formats the project owns, and close handles to affected files
  before reopening temporary files or replacing exports.
- Use argument lists with `subprocess.run` and resolve external tools with
  `shutil.which` when they are discovered on `PATH`. Keep Bash, systemd,
  and Homebrew requirements within the deployment commands that use them.
  Guard platform-specific imports and calls at the integration boundary.
- When changing a dependency with native code, check wheel availability
  against the OS, architecture, and Python versions in the
  [CI matrix](.github/workflows/ci.yml). Keep tests for shared behavior
  enabled across the matrix and reserve skips for platform-specific features.
- For platform-sensitive changes, run the offline checks in
  [Useful Commands](#useful-commands). Use native CI to verify runtime
  behavior and the installed CLI, and release smoke tests for packaging
  changes. Report which platforms actually ran the affected checks.

## Verification Checklist

For most scraper/storage/export changes, run:

```bash
uv run python -m compileall src main.py
uv run --locked poe test
uv run job-scrapper --export-only
```

For source changes, also run a small scrape:

```bash
uv run job-scrapper --results-wanted 5 --custom-pages 1
```

Then verify:

- The CLI completes even if one source fails.
- `exports/jobs.xlsx` exists.
- `Offres` has rows and `Données brutes` exists.
- `TableOffres` exists and the filter range covers all columns.
- If both `Postulé` and `Statut` are configured, the first `Postulé` data cell contains a formula referencing `Statut`.
- Apply/source link columns contain `HYPERLINK(...)` formulas when URLs exist.
- No obvious stage/alternance/apprentissage/contrat de professionnalisation offer remains.
- Salary min/max are annual numeric values or empty.

## OpenAI/Codex Guidance

Codex loads this file as project-level durable guidance. Keep it current when the repo structure, commands, or scraper contracts change.

Always use the OpenAI developer documentation MCP server if you need to work with the OpenAI API, ChatGPT Apps SDK, Codex, or related docs without the user having to explicitly ask.
