# Configuration reference

## Defaults and profiles

The bundled defaults use Toulouse as the example municipality.
See the [default configuration](../src/jobsfr/config/config.yaml).

Copy the complete YAML example to your default [configuration path](#database-export-and-log-paths)
or use it as a separate profile. Edit the search terms, title rules, and towns,
then uncomment the optional settings you want to override. [Complete YAML example](config.local.example.yaml)

Run `jobsfr setup --example` to print the same YAML from an installed build.

Run `jobsfr setup` to create your default configuration file, or add `--profile PATH` to save
the settings to a new YAML file. Pass the same `--profile PATH` to subsequent
commands to load that file in place of the default profile.
Live searches require explicit `queries` and `locations` from your settings
or command-line overrides.
Use `jobsfr status --profile PATH` to validate the profile offline. The CLI
reports duplicate YAML keys, unknown settings, and misspelled source switches.
Git ignores `config.local.yaml` and files matching
`src/jobsfr/config/*.local.yaml`. Keep credentials in `.env`.
See the [environment file example](../.env.example).

Set `JOBSFR_USE_WORKING_DIRECTORY=1` in the process environment to use working
directory paths in an installed build. Set `JOBSFR_DISABLE_LOCAL_CONFIG=1`
there to skip the default user YAML. An explicit `--profile PATH` still loads.
Use `1` or `true` to enable either flag and `0` or `false` to disable it.
The flags are read before `.env` or YAML files are loaded.

## Search terms and geography

`queries` tells boards what to search for. `filters` controls which listings
the scraper keeps. Use `include` and `exclude` lists under `title`,
`description`, `contract`, or `company`. A listing must match at
least one rule in each populated include list. Any exclude match rejects it.
Use the contract labels reported by the boards.
The built-in contract policy uses the bundled `filters.contract.exclude`
terms and checks listing titles, contract labels, and description phrases.
Profile contract rules apply to the current search and export.
Set a board's key to `false` under `enabled_sources` to skip it in future searches.
Its saved listings remain eligible for export. [Source keys](../README.md#job-sources)

Plain strings match whole words or phrases. Hyphens and spaces are equivalent,
so `porte-à-porte` also matches `porte à porte` and `porte - à - porte`.
Use `{regex: '\bporte a porte\b'}` for Python regex. Patterns run on lowercase,
unaccented text with punctuation replaced by spaces.

| Optional filter | Effect |
| --- | --- |
| `company.exclude.types` | Exclude employers by type using the community catalog. |
| `remote` | Set to `true` for remote listings or `false` for on-site listings. Listings with unknown remote status pass. |
| `salary.min_annual_eur` and `salary.max_annual_eur` | Check each available annual EUR salary bound against its corresponding limit. Either limit can be used independently. |
| `posted_after` and `posted_before` | Check available posting dates against inclusive bounds. Use dates in `YYYY-MM-DD` format. |
| `by_source` | Apply additional filter rules to listings from the source named in each key. |

Missing remote status, unavailable annual EUR salary amounts, and missing or
unreadable posting dates pass their respective filters. A missing text field
fails an include rule. Custom filters apply to new listings and tracker re-exports
from saved data. You can change them and re-export the existing DuckDB.

Set `locations` to one or more French municipality names.
Pass `--location` once per town to override the configured locations for a run.
The location filter uses `distance_km` by default. Set
`location_scope.radius_km` to give the filter its own radius and use
`location_scope.city_aliases` to add alternate municipality names. The filter
ignores differences in case, accents, and punctuation. Add an alias for a
recurring spelling variation you can verify.

By default, the tracker keeps listings with a town label the geography cache
cannot resolve. These appear in `Offres` and the selected CSV export with the
provider's town spelling. Include the optional `À vérifier` column to flag them.
Set `location_scope.include_unrecognized: false` to exclude these listings.
Blank locations, region-only labels, and towns the cache identifies as outside
the radius stay outside the tracker.

Live runs resolve each municipality's coordinates, department, and region to
build provider routes. Municipality and region data are cached beside DuckDB.
Live runs fetch missing cache entries and recalculate changes to the radius or
aliases from cached geography. Offline exports and replay can recalculate the
radius for cached towns. A live search caches towns that are new to a profile.

Use `jobsfr status` to inspect effective settings offline. Select a YAML profile
with `--profile` or a database with `--db`. Both text and JSON use the same
saved geographic scope or fall back to cached geography. Add `--json` for the
full settings and their origins. Pass one-off search overrides such as `--query`
and `--location` to `jobsfr search`.

## Keep reposts in the tracker

The tracker keeps later publications of a vacancy on separate rows by default.
Set `export.keep_reposts` to `false` to show one row per vacancy.

The tracker identifies a repost when the same board lists an already matched
vacancy under a new listing link at least seven days after its earlier
publication. A repost shows `Republié` while pending and keeps its own `Postulé`
or `Refusé` status after you update it. `Lien` opens the board's repost.
`Postuler`, if selected, opens the application link. An export with reposts adds
`Statut` and `Lien` to custom layouts that omit them.

## Excluding employer types

List the employer types you want to exclude under `filters.company.exclude.types`
in your profile. See the [complete YAML example](config.local.example.yaml).

`esn` covers IT services and technology consulting firms.
`recruitment_agency` covers recruiters, including permanent placement and executive search.
`temporary_staffing` covers intérim agencies.
Use `exclude.names` alongside `exclude.types` to also exclude companies by
name or regex.

Run `jobsfr export` to apply the filters to saved offers and the
existing tracker without scraping. Type filters compare the full employer
name after normalizing case, accents, punctuation, and common French legal
suffixes. Previously stored offers remain in DuckDB and the raw exports.

Online scrapes with company type filters check for catalog updates once per day.
They cache the last validated CSV in `.catalogs/` beside the profile's DuckDB
file. Offline runs use that cache or the bundled snapshot. The run log records
the SHA-256 of the catalog used. Set `company_catalog_path` in your profile to
pin a local CSV.

The [community employer catalog](../datasets/employers/v1/company_types.csv)
is a comma-separated UTF-8 file with `company`, `type`, and `sources` columns.
Add a row with a source link for each employer or verified alias. Use one
row per applicable type and a new value in the `type` column to introduce
a type. Updates become available to compatible installs when they merge into
the default branch. The versioned CSV path lets scraper releases fetch the
same schema independently of app releases.

The catalog schema validates employer names, lowercase snake_case type keys,
HTTP(S) source links, and unique employer/type pairs after name normalization.
Separate multiple source links with ` | `. To check the bundled catalog
independently of a search, run
`uv run --locked python -c "from jobsfr.catalogs.company_types import company_types; company_types()"`.

## Aggregator overrides

To change a built-in provider in your YAML, use its source name as a key under
`aggregators.direct_specs` or `aggregators.site_search_sources`. Keep the
existing `query_template` to reuse the search history in DuckDB.
See the [built-in provider specs](../src/jobsfr/config/aggregator_specs.py).

## Database, export, and log paths

Installed builds, including Homebrew, store exports in a `jobsfr` folder
inside your Documents folder. On Linux this follows `XDG_DOCUMENTS_DIR` from
`user-dirs.dirs` when configured. On Windows it follows the system Documents
folder, including redirects.

| Platform | Configuration file | DuckDB and its caches | Run logs |
| --- | --- | --- | --- |
| macOS | `~/.config/jobsfr/config.yaml` | `~/Library/Application Support/jobsfr` | `~/Library/Logs/jobsfr` |
| Linux | `~/.config/jobsfr/config.yaml` | `~/.local/share/jobsfr` | `~/.local/state/jobsfr` |
| Windows | `%APPDATA%\jobsfr\config.yaml` | `%LOCALAPPDATA%\jobsfr` | `%LOCALAPPDATA%\jobsfr\Logs` |

On Linux, absolute `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME`
override the corresponding roots. Installed builds read `.env` from the user
configuration folder if present, then fall back to `.env` in the working
directory. Check the configuration path with `jobsfr status`.

Source checkouts and Docker load `src/jobsfr/config/config.local.yaml` and
default to `exports/` and `data/`. Relative paths in YAML resolve from the
checkout root for source runs and the working directory for installed builds.
Absolute paths and paths beginning with `~/` work for local installations.
Give separate `paths.database_path`, `paths.export_dir`, and `paths.log_file`
values to keep profiles independent.

Use `jobsfr status --json` to verify the paths before running the scraper. [Docker setup](../deploy/README.md#docker-cli)
