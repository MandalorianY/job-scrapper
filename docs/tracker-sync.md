# Tracker sync

## Set up the local tracker

Run `jobsfr search` to create the tracker, or use `jobsfr export`
to regenerate it from an existing DuckDB database. If you cloned the repo,
run the commands from the checkout root with the `uv run --locked` prefix.

The first entry in `export.formats` is the tracker you edit and sync. By default,
the scraper writes `jobs.xlsx` and CSV files to your export folder.

Put your settings in `src/jobsfr/config/config.local.yaml` in a checkout, or
in `config.yaml` in the installed build’s [user configuration folder](configuration.md#database-export-and-log-paths).
If you run `jobsfr search --profile FILE`, pass the same `--profile FILE` to
`jobsfr sync` to use that search, database, and tracker.

In the `Offres` sheet, use the `Statut` dropdown to mark an offer `En Attente`,
`Postulé`, or `Refusé`. With the default `export.keep_reposts` setting, new
repost rows start with `Republié`. Keep `Statut` in `export.excel_columns` if you customize
the columns and want the dropdown. Save and close the workbook before syncing.
Keep the hidden `.jobsfr-tracker-history.csv` file with your local tracker when
moving or backing up the export folder. It holds rows temporarily hidden by
search filters and lets their application statuses return with the rows.

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

Copy the `sync` block from the [complete YAML example](config.local.example.yaml)
into your YAML configuration and uncomment it. Set `sync.enabled`
to `true` and use your rclone remote and tracker path for `sync.target`.

```sh
uv run --locked jobsfr sync --status
uv run --locked jobsfr sync
```

When sync is enabled, `sync --status` checks for rclone, validates the
destination, and tests remote read and write access with a temporary file.
If a check fails, it reports the problem and exits with an error.

The plain `sync` command runs the same checks before scraping. If a check fails,
it prints the reason immediately and runs the search and export locally. When
the checks pass, it carries forward application statuses and uploads the refreshed tracker.
The first successful sync can create the destination file.

Set `sync.enabled: false` in YAML to disable sync. JobsFR records each
database's last successful sync in
`.tracker-sync/databases/<database filename>/settings.json` beside the database.

For Homebrew, use `jobsfr` in place of `uv run --locked jobsfr`.
For a standalone build, use the executable from your archive, such as
`.\jobsfr.exe` in PowerShell.

Match the remote file extension to the first export format. For ODS, put `ods`
first, use `jobs.ods` as the destination, and add `--extra ods` to each `uv run`
command. For CSV, use the localized `jobs.csv` as the destination. If you change
the first format, update `sync.target` with the new destination.
