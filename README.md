# Job Scrapper

A job scraper for France that deduplicates listings, filters out student contracts, and preserves application statuses across Excel exports.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run from the checkout root. The project defaults to Python 3.14 and requires at least 3.13.

```sh
uv run --locked job-scrapper
```

## Release downloads

Each [release](https://github.com/MandalorianY/job-scrapper/releases) lists the platform and architecture in the asset name. Verify downloads against `SHA256SUMS`.

| Format | How to run |
| --- | --- |
| Windows `.exe` | Download the `windows-amd64` or `windows-arm64` executable and run it from PowerShell with `--help`. |
| Debian `.deb` | Install the `amd64` or `arm64` package with `sudo apt install ./job-scrapper_VERSION_ARCH.deb`, then run `job-scrapper`. |
| Linux or macOS binary tarball | Extract the platform-matching `job-scrapper-VERSION-PLATFORM-ARCH.tar.gz` and run the executable inside. Keep it alongside the `_internal` directory. |
| Python wheel or source tarball | Install the wheel with `uv tool install --python 3.14 ./scrapper-VERSION-py3-none-any.whl`. The `scrapper-VERSION.tar.gz` source archive can be unpacked and run with `uv run --locked job-scrapper`. |

The standalone releases bundle Python. Linux binaries require glibc 2.35 or later. Run the CLI from your tracker directory or use `--profile FILE` and explicit output paths. Use `--version` to confirm which installation your shell found.

## Homebrew

Install the latest published release from the project's tap.

```sh
brew install mandaloriany/tap/job-scrapper
```

Run `job-scrapper` from the directory where you want to keep `data/` and `exports/`. Pass `--profile /path/to/profile.yaml` to use your own search settings.
You can also put `config.local.yaml` in that directory to load it automatically.

Use `job-scrapper-install-timer --profile /path/to/profile.yaml` to schedule recurring runs with Homebrew services. [Schedule configuration](deploy/README.md)

If the Docker launcher takes precedence on your `PATH`, run `"$(brew --prefix job-scrapper)/bin/job-scrapper"` to select the Homebrew installation.

## Docker CLI

With Docker running, start a live scrape from the checkout root.

```sh
./deploy/run-docker.sh
```

The launcher builds the image and uses the checkout’s local config, `data/`, and `exports/`.
Use `--profile PATH` or `--replay-from PATH` to pass in a host file. Relative input paths are looked up from the calling directory first, then from the checkout. Use relative YAML output paths under `data/` or `exports/` to keep them in the checkout. For output elsewhere on the host, pass an absolute path with `--db`, `--export-dir`, or `--log-file`.

To run `job-scrapper` from any directory, link the launcher into `~/.local/bin` if it’s on your `PATH`.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/deploy/run-docker.sh" "$HOME/.local/bin/job-scrapper"
```

Recreate the link if you move the checkout.

## Configuration

The bundled configuration uses Toulouse as its example town. Set your roles and one or more French municipalities in `src/scrapper/config/config.local.yaml`.

```yaml
queries: [data analyst]
include_terms: [data analyst, analyste de données]
locations: ["Nantes, France"]
distance_km: 10
```

`queries` tells boards what to search for. `include_terms` controls which job titles pass the filter. For live searches the CLI resolves each municipality's coordinates, department, and region to build provider routes. The location filter uses `distance_km` unless `location_scope.radius_km` is set separately. Pass `--location` once per town to override the saved locations for a run.

The bundled Toulouse Graphiste profile searches for graphic design, motion design, and PAO roles within 25 km of Toulouse. Save another search as a separate YAML file and select it with `--profile`. The chosen file takes the place of `config.local.yaml`. Set `paths.database_path`, `paths.export_dir`, and `paths.log_file` to separate paths for each profile. Files matching `src/scrapper/config/*.local.yaml` are ignored by Git.

Relative YAML output paths resolve from the checkout when using `uv`, or from the working directory when using an installed CLI. Absolute paths and `~/` work for local installations.

```sh
uv run --locked job-scrapper --profile src/scrapper/config/toulouse-graphiste.yaml --status
uv run --locked job-scrapper --profile src/scrapper/config/toulouse-graphiste.yaml
```

Use `--show-config` to inspect settings, their origins, and available cached geography offline. For French searches, set the town names in `locations` and optionally list alternate names under `location_scope.city_aliases`. Live runs cache municipality and region data beside DuckDB. They look up only towns and regions missing from that cache and recalculate changes to the radius or aliases locally. The resolved scope is saved for offline exports and replay with the same location settings. Keep credentials in `.env`. See the [example configuration](src/scrapper/config/config.yaml) and [.env.example](.env.example).

## Job sources

Use `--status` for an offline summary of search settings, enabled sources, and tracker sync. It uses saved geographic scope when available. Set a source to `false` under `enabled_sources` in `config.local.yaml` to disable it.

Built-in aggregator routes live in Python. To override one provider in a profile, use its source name as a key under `aggregators.direct_specs` or `aggregators.site_search_sources`. Keep existing `query_template` values to reuse the search history in DuckDB.

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

Use `--help` for available options or `--export-only` to regenerate files from an existing DuckDB without scraping.

## Replay and recovery

Rebuild from up to 90 days of saved observations using the current filters.

```sh
uv run job-scrapper --replay-from data/job_search.duckdb --db data/rebuilt.duckdb --export-dir exports/rebuilt
```

Keep the previous workbook to carry application statuses into a rebuilt tracker. Query DuckDB’s `source_health` view to diagnose source failures.

## Development

```sh
uv run --locked poe test
```

Poe runs Ruff before pytest. Pytest writes the HTML coverage report to `htmlcov/index.html`. Set `SCRAPPER_RUN_REAL_SMOKE=1` to include live smoke tests.

Run `uv run --locked poe lint` for lint checks or `uv run --locked poe format` to apply Ruff fixes and formatting.

### Releases

After CI passes on `main`, tag that commit with the version in `pyproject.toml`. For example:

```sh
git tag -a v0.1.0 -m "Release v0.1.0"
git push origin refs/tags/v0.1.0
```

The [release workflow](.github/workflows/release.yml) builds the Python distributions, Windows executables, Debian packages, and portable Linux and macOS archives. It smoke-tests them on their build platforms before attaching the assets and checksums to a GitHub release draft. Review the generated notes and publish the draft with `gh release edit v0.1.0 --draft=false`.

Publishing the latest release updates the [Homebrew tap](https://github.com/MandalorianY/homebrew-tap) with the source archive's SHA-256. The [tap workflow](.github/workflows/homebrew.yml) uses the `HOMEBREW_TAP_DEPLOY_KEY` Actions secret to push the formula to the tap.

[Scheduled runs](deploy/README.md) · [MIT License](LICENSE)
