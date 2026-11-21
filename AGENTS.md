# AGENTS.md

## Project Purpose

This repository is a Python job-search scraper for configured example roles in Example Region, Example Country. It stores incremental raw offers in DuckDB, dedupes offers across sources, filters out internships/apprenticeships/alternance, and exports a localized Excel tracker plus CSV files.

Primary outputs are ignored runtime artifacts:

- `data/job_search.duckdb`
- `data/job-scrapper.jsonl`
- `exports/jobs.xlsx`
- `exports/jobs.csv`
- `exports/jobs_raw.csv`

Do not commit generated data or exports unless the user explicitly asks for that.

## Stack

- Python 3.14 with `uv` by default, with Python 3.13 as the minimum supported version
- Typer CLI exposed as `job-scrapper`
- DuckDB for local storage and incremental search state
- JobSpy for supported boards: Indeed, LinkedIn, Glassdoor, Google Jobs, HelloWork
- `httpx` + BeautifulSoup for APIs, aggregator pages, and web search
- OpenPyXL and pandas for localized Excel/CSV exports

The tracked `src/scrapper/config/config.yaml` is the public example template. Use `src/scrapper/config/config.local.yaml` (when present) for live repository settings such as query/location tuning and source-specific overrides.

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

Run lint plus pytest with coverage:

```bash
uv run --locked --group test task test
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

- `src/scrapper/config/`: Pydantic Settings models plus packaged `config.yaml` defaults and `config.dev.yaml` / `config.prod.yaml` overrides for queries, include/exclude terms, source lists, limits, paths, user agent, and source-specific endpoints. Secrets belong in `.env`.
- `src/scrapper/models.py`: `JobOffer`, the single normalized offer shape used by every source.
- `src/scrapper/storage.py`: DuckDB schema, incremental search state, upsert, dedupe, canonical/raw dataframes, purge filters.
- `src/scrapper/text.py`: text cleanup, URL keys, stable hashes, dedupe key, include/exclude matching, completeness scoring.
- `src/scrapper/export.py`: localized Excel and CSV export, salary normalization, formulas, filters, tables, colors.
- `src/scrapper/cli.py`: orchestration and summary output.
- `src/scrapper/scrapers/`: one module per scraper family.

Keep these responsibilities separate. New source-specific parsing belongs in `src/scrapper/scrapers/<source>.py`; shared extraction helpers belong in `scrapers/common.py`; general text/filter/dedupe behavior belongs in `text.py`; persistent schema/query behavior belongs in `storage.py`.

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
Contract cleanup applies to the whole canonical duplicate group, so a sparse copy
cannot hide internship evidence from another source. Purged URL keys are retained
in `excluded_offer_urls` to prevent historical tracker copies from restoring them.

Be careful with false positives. Do not remove an offer just because a description mentions managing an intern/alternant, internal example role, or international work. The stricter contract-stage logic lives in `text.py`; extend the phrase patterns there with focused examples when needed.

For broad sources, keep the include filter title-focused unless there is an explicit product decision to broaden it. This prevents generic "responsable" or non-configured example roles from leaking into the workbook.

## Dedupe And Completeness

Deduping matches provider IDs and listing-specific URLs first. Title/company and description matches across boards also consider location with the default settings. The canonical export ranks source priority, completeness, and scrape time, and preserves duplicate counts and links.

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

Search attempts keep their run ID, outcome, and seen count in DuckDB. `source_health` shows the latest attempt and successful watermark. The 90-day observation archive records offers accepted at ingestion and supports offline `--replay-from`.

Custom/detail scrapers should skip known detail pages by default using `store.source_url_seen(...)`, unless `config.refresh_known_details` is true. Search/list pages may still be revisited to discover new links.

## JobSpy

Use JobSpy for sources in `config.jobspy_sites`: Indeed, LinkedIn, Glassdoor, Google Jobs, HelloWork.

Google is intentionally attempted through JobSpy first. Direct JobSpy Google Jobs may still return 0 results with `initial cursor not found`; when that happens, preserve the search-state error and continue with the other free sources. Do not invoke a paid Google Jobs fallback from the CLI unless paid API usage is explicitly requested.

For JobSpy changes:

- Keep `hours_old` incremental with the configured overlap.
- Set JobSpy's `fetch_description` for LinkedIn on a per-board copy of the search input so other boards in a batch keep their normal request count.
- Run HelloWork separately with detail fetching enabled and convert the configured search radius to whole miles for JobSpy.
- Keep Google-specific query construction in `_google_search_term`.
- Normalize all row values before building `JobOffer`.
- Filter before upsert.

## Google Jobs And Site-Scoped Search

Google Jobs uses JobSpy first. The CLI should stay free by default and should not call paid search APIs. Never hardcode keys.

Supported aggregators discover offers through their own APIs, search pages, or sitemaps. Treat missing direct routes and source failures as non-fatal and record their errors in `search_state`.

When adding site-scoped searches:

- Exclude the main boards already scraped directly.
- Reject informational pages such as job guides, salary pages, training pages, and job-description articles.
- Require a regional signal for Example Region.
- Require a example-role title.

## Aggregators And Custom Sources

Aggregator scrapers are intentionally mixed:

- Direct HTML/API extraction for sources with usable listing/detail pages.
- Site-scoped web search for sources that are harder to crawl consistently.

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

Generate the workbook and both CSV files in staging before replacing the published files.

After export changes, regenerate and inspect `exports/jobs.xlsx` with OpenPyXL.

## Coding Style

- Keep code small, readable, and source-specific.
- Annotate function parameters and return types, including private helpers and tests. Ruff enforces the annotations.
- Prefer reusable helpers over repeated parsing snippets.
- Prefer structured data such as JSON-LD, API JSON, and known IDs before scraping loose visible text.
- Use standard library parsing utilities for URLs, dates, and JSON rather than fragile string slicing when practical.
- Catch source-level scraper exceptions so other sources and export still run.
- Do not add new production dependencies unless they materially simplify the scraper.
- Do not store secrets in code, `.env`, DuckDB, exports, or logs.

## Verification Checklist

For most scraper/storage/export changes, run:

```bash
uv run python -m compileall src main.py
uv run --locked --group test task test
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
