# Tracker sync

Tracker sync uses the first format in `export.formats` as the editable remote
tracker. Set up a writable rclone remote and use an existing folder on it. Run
sync from the same directory as your usual scraper command so it uses the same
configuration and database.

On Windows, install rclone with `winget install Rclone.Rclone`, then open a new
PowerShell window. Run `rclone config` to name and authorize your remote. Use `/`
between folders in remote paths. See the [Google Drive setup](https://rclone.org/drive/)
and [OneDrive setup](https://rclone.org/onedrive/) guides.

With a standalone build that includes `sync`, use the executable name from your
archive. This example uses the default XLSX tracker and a remote named `mydrive`.

```powershell
rclone listremotes
.\job-scrapper-0.1.0-windows-amd64.exe sync --enable "mydrive:Job Search/jobs.xlsx"
.\job-scrapper-0.1.0-windows-amd64.exe sync --status
.\job-scrapper-0.1.0-windows-amd64.exe sync
```

Save and close the local workbook before running sync so Windows can replace it.
From a checkout, use `uv run --locked job-scrapper sync` in place of the
standalone executable. For ODS, set `ods` first in `export.formats` and use the
checkout with the `ods` extra.

```sh
uv run --locked --extra ods job-scrapper sync --enable "mydrive:Job Search/jobs.ods"
uv run --locked --extra ods job-scrapper sync
```

For XLSX or CSV as the first format, use a `.xlsx` or `.csv` destination.
CSV sync uses the localized `jobs.csv` tracker. Other selected exports are
generated locally from the same application statuses. If you change the first
format, enable a destination with the new extension.
