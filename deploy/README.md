# Deployment

## Scheduled runs

Set the days and time in your YAML profile or checkout's `config.local.yaml`. The default is weekdays at 15:30. Use `mon`, `tue`, `wed`, `thu`, `fri`, `sat`, or `sun` and a quoted 24-hour time.

```yaml
schedule:
  days: [mon, wed, fri]
  time: "09:15"
```

Run the installer again after changing the schedule. The same profile is passed to the scraper on each run.

### Export folder

Scheduled runs write `jobs.xlsx`, `jobs.csv`, and `jobs_raw.csv` to `paths.export_dir` in your configuration. The default is `exports/`. When you use `--profile`, set the folder in that profile.

- On macOS, `exports/` and other relative paths are based on the directory where you run `job-scrapper-install-timer`.
- On Linux, relative paths are based on the repository checkout.

Use an absolute path or a path starting with `~/` to choose a fixed folder.

```yaml
paths:
  export_dir: ~/Documents/Job Search/exports
```

### macOS with Homebrew

Install the formula and run the timer installer from the directory where you keep your tracker. Pass `--profile` to use your own search and schedule settings.

```sh
brew install mandaloriany/tap/job-scrapper
job-scrapper-install-timer --profile /path/to/profile.yaml
brew services list
```

The service uses the Mac's time zone and runs as your logged-in user. Run `brew services stop job-scrapper` to disable it. Console output goes to `data/service.log` in the directory where you ran the installer. To update the schedule, run the installer again from that directory with the profile.

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

To run `job-scrapper` from any directory, link the launcher into `~/.local/bin`
if it’s on your `PATH`.

```sh
mkdir -p "$HOME/.local/bin"
ln -s "$PWD/deploy/run-docker.sh" "$HOME/.local/bin/job-scrapper"
```

Recreate the link if you move the checkout.
