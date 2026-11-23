# Deployment

## Scheduled runs

Set the days and time in your YAML profile or `config.local.yaml`. The default is weekdays at 15:30. Use `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, or `sun` and a quoted 24-hour time.

```yaml
schedule:
  days: [mon, wed, fri]
  time: "09:15"
```

Run the installer again after changing the schedule. The same profile is passed to the scraper on each run.

### Export folder

Scheduled runs write `jobs.xlsx`, `jobs.csv`, and `jobs_raw.csv` to `paths.export_dir` in your configuration.

- On macOS, the default is `~/Documents/job-scrapper`. Relative overrides are based on the directory where you run `jobsfr-install-timer`.
- For a Linux checkout, the default is `exports/` and relative overrides are based on the repository checkout.

Use an absolute path or a path starting with `~/` to choose a fixed folder.

```yaml
paths:
  export_dir: ~/Documents/Job Search/exports
```

### macOS with Homebrew

Install the formula and run the timer installer. Pass `--profile` or save your search and schedule settings in `~/Library/Application Support/job-scrapper/config.local.yaml`.

```sh
brew install mandaloriany/tap/jobsfr
jobsfr-install-timer --profile /path/to/profile.yaml
brew services list
```

The service uses the Mac's time zone and runs as your logged-in user. Run `brew services stop jobsfr` to disable it. The timer installer writes a service that sends console output to `~/Library/Logs/job-scrapper/service.log`. To update the schedule, run the installer again with the same profile or local configuration.

If you set up a timer under the old formula, run `brew services stop job-scrapper`
before upgrading. Upgrade an existing Homebrew install with these commands.

```sh
brew update
brew migrate --formula mandaloriany/tap/job-scrapper
brew upgrade mandaloriany/tap/jobsfr
```

If you used the old timer, run `jobsfr-install-timer` again from the same directory
with your original profile, if any. Existing scripts can still invoke `job-scrapper`.
See the [path settings](../docs/configuration.md#database-export-and-log-paths) to
reuse a tracker and DuckDB database from an earlier release.

### Linux with systemd

Install `uv` and make sure a systemd user session and `flock` are available. From your checkout, run:

```sh
./deploy/install-timer.sh
```

The installer uses the checkout's path, enables the timer, and prints the next run. Add `--profile /path/to/profile.yaml` to use a specific profile. The timer uses Europe/Paris time and skips missed runs. The wrapper uses `flock` to prevent overlapping runs. Run `systemctl --user disable --now job-scrapper.timer` to disable the timer.

Put optional environment overrides in `~/.config/job-scrapper/environment` with mode `600`. Keep the checkout's `data/`, `exports/`, and local configuration when updating the source.

Read console output in `data/service.log` and structured events in `data/job-scrapper.jsonl`. If the timer must run while you are logged out, enable lingering for the deployment user.

## Docker CLI

With Docker running, start a live scrape from the checkout root.

```sh
./deploy/run-docker.sh
```

The launcher builds the image and uses the checkout’s local config, `data/`, and
`exports/`.
Use `--profile PATH` or `--replay-from PATH` to pass in a host file. Relative
input paths are looked up from the calling directory first, then from the
checkout. Use relative YAML output paths under `data/` or `exports/` to keep
them in the checkout. For output elsewhere on the host, pass an absolute path
with `--db`, `--export-dir`, or `--log-file`.
To pin an employer catalog, put its CSV under the mounted `data/` directory
and set `company_catalog_path` to that relative path in your profile.

To run `jobsfr` from any directory, link the launcher into `~/.local/bin`
if it’s on your `PATH`.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/deploy/run-docker.sh" "$HOME/.local/bin/jobsfr"
```

Recreate the link if you move the checkout.
