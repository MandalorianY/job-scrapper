# Deployment

## Scheduled runs

Create your search settings with `jobsfr setup` or supply a YAML profile with
`queries` and `locations`. Run `jobsfr schedule enable` to choose your days and
time and start automatic searches. Run `jobsfr schedule disable` to stop them.

The prompts start with weekdays at 15:30. If your YAML already has a schedule,
choose whether to enable it as saved or modify it first. Use comma-separated
days from `mon` through `sun` and a 24-hour time.

```yaml
schedule:
  days: [mon, wed, fri]
  time: "09:15"
```

After the timer is enabled, new choices are saved to the selected `--profile`
file, `config.local.yaml` in a checkout, or the installed user's `config.yaml`.
For scripts, `jobsfr schedule enable --yes` applies the configured values
without prompts. Disabling the timer keeps the saved schedule for later.

### Export folder

Scheduled runs use `paths.export_dir` from your configuration. The timer starts from a fixed directory on each platform.

| Platform | Default export folder | Base for relative paths |
| --- | --- | --- |
| macOS with Homebrew | `~/Documents/jobsfr` | `~/Library/Application Support/jobsfr` |
| Linux with systemd | `exports/` in your checkout | Repository checkout |

Use absolute paths or paths starting with `~/` to share the same database and exports between scheduled runs and commands you launch from other directories.

```yaml
paths:
  export_dir: ~/Documents/Job Search/exports
```

### macOS with Homebrew

Install the formula and run `jobsfr schedule enable`. Pass `--profile` or save your search and schedule settings in `~/.config/jobsfr/config.yaml`.

```sh
brew install mandaloriany/tap/jobsfr
jobsfr schedule enable --profile /path/to/profile.yaml
brew services list
```

The service uses the Mac's time zone and runs as your logged-in user. It sends console output to `~/Library/Logs/jobsfr/service.log`.

### Linux with systemd

Install `uv` and make sure a systemd user session and `flock` are available. From your checkout, run:

```sh
uv run --locked jobsfr schedule enable
```

The installer uses the checkout's path, enables the timer, and prints the next run. Add `--profile /path/to/profile.yaml` to use a specific profile. The timer uses Europe/Paris time and skips missed runs. The wrapper uses `flock` to prevent overlapping runs. Run `uv run --locked jobsfr schedule disable` to disable the timer from a checkout.

Put optional environment overrides in `~/.config/jobsfr/environment` with mode `600`. Keep the checkout's `data/`, `exports/`, and local configuration when updating the source.

Read console output in `data/service.log` and structured events in `data/jobsfr.jsonl`. If the timer must run while you are logged out, enable lingering for the deployment user.

## Docker CLI

With Docker running, start a live scrape from the checkout root.

```sh
./deploy/run-docker.sh search
```

The launcher builds the image and uses the checkout’s local config, `data/`, and
`exports/`.
Use `--profile PATH` or `replay --archive PATH` to pass in a host file. Relative
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
