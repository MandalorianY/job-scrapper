# Job Scraper for France | Automated Job Search & Excel Application Tracker

Open-source Python tool to automate job searches in France and track applications in Excel.

Search job listings across France Travail, Indeed, LinkedIn, HelloWork, Welcome to the Jungle, Glassdoor, and other supported French job boards from one configurable workflow.

Find relevant jobs by job title, city, and search radius. Choose your sources, filter unwanted keywords and contract types (including internships and apprenticeships, full-time and temporary contracts), and deduplicate listings across job boards.
Optionally exclude IT consulting firms (ESNs), recruitment agencies, or specific companies to prioritize direct-client opportunities. Contribute to the community-maintained employer catalog.


Export results to Excel (.xlsx), CSV, or LibreOffice (.ods). Manage your applications in an editable spreadsheet and preserve your application statuses between searches, even when new job listings are added.

Additional capabilities include scheduled job searches, multiple YAML search profiles and optional private cloud synchronization with rclone.

**🇫🇷 En français**

Outil Python open source pour automatiser vos recherches d'emploi en France et suivre vos candidatures dans Excel.

Recherchez des offres d'emploi sur France Travail, Indeed, LinkedIn, HelloWork, Welcome to the Jungle, Glassdoor et d'autres sites d'emploi français à partir d'un seul outil configurable.

Trouvez des offres pertinentes par métier, ville et rayon géographique. Choisissez vos sources, filtrez les mots-clés et les types de contrat qui ne vous intéressent pas (CDI, CDD, stage, alternance) et éliminez les offres en doublon entre les différentes plateformes.
Excluez, si vous le souhaitez, les entreprises de services du numérique (ESN), les cabinets de recrutement ou certaines entreprises afin de privilégier les opportunités en direct avec les clients finaux. **Contribuez à l’annuaire d’employeurs maintenu par la communauté.**


Exportez les résultats vers Excel (.xlsx), CSV ou LibreOffice (.ods). Gérez vos candidatures dans un tableau modifiable et conservez vos statuts de candidature entre chaque recherche, même lorsque de nouvelles offres sont ajoutées.

Parmi les fonctionnalités supplémentaires, l'exécution planifiée des recherches d'emploi, la gestion de plusieurs profils de recherche au format YAML et la synchronisation facultative et privée avec un service cloud via rclone.

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run from the checkout root. The project defaults to Python 3.14 and requires at least 3.13.

```sh
uv run --locked jobsfr
```

Download a build for your platform from [GitHub Releases](https://github.com/MandalorianY/job-scrapper/releases).

### Docker CLI

With Docker running, run `./deploy/run-docker.sh` from the checkout root.
See [Docker setup](deploy/README.md#docker-cli) for host paths and launcher setup.

## Homebrew

Install the latest published release from the project's tap.

```sh
brew install mandaloriany/tap/jobsfr
```

See [Homebrew upgrades](deploy/README.md#macos-with-homebrew) if you installed `job-scrapper` before the rename.

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

## Configuration and Quick start

Save your search settings in `config.local.yaml`.

- **Checkout and Docker** use `src/scrapper/config/config.local.yaml`.
- **Homebrew and standalone builds** use your platform’s [user configuration folder](docs/configuration.md#database-export-and-log-paths). Run `jobsfr --status` to see where to save `config.local.yaml`.

```yaml
# Send these searches to the job boards.
queries: [data analyst, analyste de données, analyste BI]

filters:
  title:
    include: [data analyst, analyste de données, analyste BI, BI analyst]
    exclude: [chef-de-projet, {regex: '\b(?:senior|lead)\b'}]
  description:
    exclude: [déplacements fréquents, porte-à-porte]
  contract:
    exclude: [intérim]

locations: ["Toulouse, France"]
distance_km: 10

# Default contract filters skip stage, alternance, and apprentissage.
```

To exclude employers by type, list one or more of `esn`, `recruitment_agency`,
and `temporary_staffing` under `filters.company.exclude.types`. Online scrapes
with the filter enabled check for catalog updates at most once every 24 hours,
so you can pick up new classifications between scraper releases. See the
[employer-type filter guide](docs/configuration.md#excluding-employer-types)
for the YAML example, offline behavior, and pinning a local catalog.

Use `--profile` to select a separate YAML file. Preview the bundled Toulouse Graphiste profile with this command.

```sh
uv run --locked jobsfr --profile src/scrapper/config/toulouse-graphiste.yaml --status
```

See the [configuration reference](docs/configuration.md) for filters, geography, aggregator overrides, and file paths.

## Results

Installed builds save the tracker and CSV files in `job-scrapper` inside your
Documents folder. The paths below are relative to the checkout for source and
Docker runs. Run `jobsfr --show-config` to see your effective paths.

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
uv run --locked --extra ods jobsfr --export-only
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
