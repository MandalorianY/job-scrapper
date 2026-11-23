# Tracker sync

## Set up the local tracker

Run `jobsfr` to create the tracker, or use `jobsfr --export-only`
to regenerate it from an existing DuckDB database. If you cloned the repo,
run the commands from the checkout root with the `uv run --locked` prefix.

The first entry in `export.formats` is the tracker you edit and sync. By default,
the scraper writes `jobs.xlsx` and CSV files to your export folder.

```yaml
export:
  formats: [xlsx, csv]
```

Put your settings in `src/scrapper/config/config.local.yaml` in a checkout, or
in the installed build’s [user configuration folder](configuration.md#database-export-and-log-paths).
If you normally pass `--profile`, put the same search settings, `paths`, and
`export` settings in `config.local.yaml` before using `sync`.

In the `Offres` sheet, use the `Statut` dropdown to mark an offer `En Attente`,
`Postulé`, or `Refusé`. With the default `export.keep_reposts` setting, new
repost rows start with `Republié`. Keep `Statut` in `export.excel_columns` if you customize
the columns and want the dropdown. Save and close the workbook before syncing.

## Connect a remote

Install rclone and run `rclone config` to name and authorize a writable remote.
On Windows, run `winget install Rclone.Rclone` and open a new PowerShell window.
See the [rclone installation guide](https://rclone.org/install/),
[Google Drive setup](https://rclone.org/drive/), and
[OneDrive setup](https://rclone.org/onedrive/).

Check that your remote appears in `rclone listremotes`. If you named it
`mydrive`, create the destination folder with `rclone mkdir "mydrive:Job Search"`
or use an existing writable folder. Use `/` between folders in the remote path.

## Enable, check, and run sync

Add this block to `config.local.yaml`, using your rclone remote and tracker path.

```yaml
sync:
  enabled: true
  target: "mydrive:Job Search/jobs.xlsx"
```

```sh
uv run --locked jobsfr sync --status
uv run --locked jobsfr sync
```

When sync is enabled, `sync --status` checks for rclone, validates the
destination, and tests remote read and write access with a temporary file.
If a check fails, it reports the problem and exits with an error.

The plain `sync` command runs the same checks before scraping. If a check fails,
it prints the reason immediately and runs a local export. When the checks pass,
it carries forward application statuses and uploads the refreshed tracker.
The first successful sync can create the destination file.

Set `sync.enabled: false` in YAML to disable sync. To manage sync from the CLI
instead, omit the `sync` block and use `sync --enable "mydrive:Job Search/jobs.xlsx"`
or `sync --disable`. The CLI stores those settings in `.tracker-sync/settings.json`
beside your database.

For Homebrew, use `jobsfr` in place of `uv run --locked jobsfr`.
For a standalone build, use the executable from your archive, such as
`.\jobsfr.exe` in PowerShell.

Match the remote file extension to the first export format. For ODS, put `ods`
first, use `jobs.ods` as the destination, and add `--extra ods` to each `uv run`
command. For CSV, use the localized `jobs.csv` as the destination. If you change
the first format, update `sync.target` or run `sync --enable` with the new
destination if you manage sync from the CLI.
