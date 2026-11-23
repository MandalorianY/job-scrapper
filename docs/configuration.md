# Configuration reference

## Defaults and profiles

The bundled defaults use Toulouse as the example municipality. The Toulouse
Graphiste profile searches for graphic design, motion design, and PAO roles
within 25 km of Toulouse. See the [default configuration](../src/scrapper/config/config.yaml)
and the [Toulouse Graphiste profile](../src/scrapper/config/toulouse-graphiste.yaml).

`--profile PATH` loads the selected YAML in place of `config.local.yaml`.
Git ignores `config.local.yaml` and files matching
`src/scrapper/config/*.local.yaml`. Keep credentials in `.env`.
See the [environment file example](../.env.example).

## Search terms and geography

`queries` tells boards what to search for. `filters` controls which listings
the scraper keeps. Use `include` and `exclude` lists under `title`,
`description`, `contract`, `company`, or `source`. A listing must match at
least one rule in each populated include list. Any exclude match rejects it.
Use the source keys from the README and the contract labels reported by the boards.
The built-in contract policy also skips internships and apprenticeships.
Use `enabled_sources` to choose which boards the scraper contacts.

Plain strings match whole words or phrases. Hyphens and spaces are equivalent,
so `porte-à-porte` also matches `porte à porte` and `porte - à - porte`.
Use `{regex: '\bporte a porte\b'}` for Python regex. Patterns run on lowercase,
unaccented text with punctuation replaced by spaces.

| Optional filter | Effect |
| --- | --- |
| `company.exclude.types` | Exclude employers by type using the community catalog. |
| `remote: true` or `remote: false` | Accept remote or on-site listings, respectively, and keep listings with unknown remote status. |
| `salary: {min_annual_eur: 40000, max_annual_eur: 70000}` | Check each available annual EUR salary bound against its corresponding limit. Either limit can be used independently. |
| `posted_after: 2026-01-01` and `posted_before: 2026-12-31` | Check available posting dates against the inclusive bounds. |
| `by_source: {linkedin: {company: {exclude: [Example Company]}}}` | Apply these additional rules to LinkedIn listings. |

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

Use `--show-config` to inspect effective settings, their origins, and available
cached geography offline. `--status` uses saved geographic scope when
available.

## Keep reposts in the tracker

The tracker keeps later publications of a vacancy on separate rows by default.
To show one row per vacancy, add this to your profile.

```yaml
export:
  keep_reposts: false
```

The tracker identifies a repost when the same board lists an already matched
vacancy under a new listing link at least seven days after its earlier
publication. A repost shows `Republié` while pending and keeps its own `Postulé`
or `Refusé` status after you update it. `Lien` opens the board's repost.
`Postuler`, if selected, opens the application link. An export with reposts adds
`Statut` and `Lien` to custom layouts that omit them.

## Excluding employer types

List the employer types you want to exclude in your profile.

```yaml
filters:
  company:
    exclude:
      types:
        - esn
        - recruitment_agency
        - temporary_staffing
```

`esn` covers IT services and technology consulting firms.
`recruitment_agency` covers recruiters, including permanent placement and executive search.
`temporary_staffing` covers intérim agencies. Select both agency types to
cover the employers previously grouped under `recruitment_agency`.
Use `exclude.names` alongside `exclude.types` to also exclude companies by
name or regex. The existing `company.exclude: [Example Company]` form still
works for name rules alone.

Run `jobsfr --export-only` to apply the filters to saved offers and the
existing tracker without scraping. Type filters compare the full employer
name after normalizing case, accents, punctuation, and common French legal
suffixes. Previously stored offers remain in DuckDB and the raw exports.

Online scrapes with company type filters check for catalog updates once per day.
They cache the last validated CSV in `.catalogs/` beside the profile's DuckDB
file. Offline runs use that cache or the bundled snapshot. The run log records
the SHA-256 of the catalog used. Set `company_catalog_path: path/to/company_types.csv`
in your profile to pin a local CSV.

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
`uv run --locked python -c "from scrapper.company_types import company_types; company_types()"`.

## Aggregator overrides

To change a built-in provider in your YAML, use its source name as a key under
`aggregators.direct_specs` or `aggregators.site_search_sources`. Keep the
existing `query_template` to reuse the search history in DuckDB.
See the [built-in provider specs](../src/scrapper/config/aggregator_specs.py).

## Database, export, and log paths

Installed builds, including Homebrew, store exports in a `job-scrapper` folder
inside your Documents folder. On Linux this follows `XDG_DOCUMENTS_DIR` from
`user-dirs.dirs` when configured. On Windows it follows the system Documents
folder, including redirects.

| Platform | `config.local.yaml` and `.env` | DuckDB and its caches | Run logs |
| --- | --- | --- | --- |
| macOS | `~/Library/Application Support/job-scrapper` | `~/Library/Application Support/job-scrapper` | `~/Library/Logs/job-scrapper` |
| Linux | `~/.config/job-scrapper` | `~/.local/share/job-scrapper` | `~/.local/state/job-scrapper` |
| Windows | `%APPDATA%\job-scrapper` | `%LOCALAPPDATA%\job-scrapper` | `%LOCALAPPDATA%\job-scrapper\Logs` |

On Linux, absolute `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME`
override the corresponding roots. Installed builds also look in the working
directory for `config.local.yaml` and `.env` if the corresponding files are
absent from the user configuration folder. Create the configuration folder
before saving either file there. Check the location with `jobsfr --status`.

Source checkouts and [Docker](../deploy/README.md#docker-cli) default to `exports/`
and `data/`. Relative paths in YAML resolve from the checkout root for source
runs and the working directory for installed builds. Absolute paths and paths
beginning with `~/` work for local installations. Give separate
`paths.database_path`, `paths.export_dir`, and `paths.log_file` values to keep
profiles independent.

If an earlier release stored your database and tracker in the working directory,
set `paths.database_path` and `paths.export_dir` to their existing locations to
reuse your listings and application statuses. Use `jobsfr --show-config`
to verify the paths before running the scraper.
