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

`queries` tells boards what to search for. `include_terms` controls which job
titles pass the filter. Set `locations` to one or more French municipality names.
Pass `--location` once per town to override the configured locations for a run.
The location filter uses `distance_km` by default. Set
`location_scope.radius_km` to give the filter its own radius and use
`location_scope.city_aliases` to add alternate municipality names.

Live runs resolve each municipality's coordinates, department, and region to
build provider routes. Municipality and region data are cached beside DuckDB.
Live runs fetch missing cache entries and recalculate changes to the radius or
aliases from cached geography. The scraper saves the resolved scope for offline
export and replay with matching location settings.

Use `--show-config` to inspect effective settings, their origins, and available
cached geography offline. `--status` uses saved geographic scope when
available.

## Aggregator overrides

To change a built-in provider in your YAML, use its source name as a key under
`aggregators.direct_specs` or `aggregators.site_search_sources`. Keep the
existing `query_template` to reuse the search history in DuckDB.
See the [built-in provider specs](../src/scrapper/config/aggregator_specs.py).

## Database, export, and log paths

Use separate values for `paths.database_path`, `paths.export_dir`, and
`paths.log_file` to keep profiles independent. `paths.export_dir` defaults to
`exports`.

Relative paths in YAML resolve from the checkout root when running from source
and from the working directory when using an installed CLI. Absolute paths and
paths beginning with `~/` work for local installations. See the path rules for
[Docker](../deploy/README.md#docker-cli) and [scheduled runs](../deploy/README.md#export-folder).
