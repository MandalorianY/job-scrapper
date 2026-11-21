# Schedule runs with systemd

The user timer runs the scraper at 15:30 Europe/Paris on weekdays. It skips missed runs. The wrapper uses `flock` to prevent overlapping runs.

Clone the repository to `~/apps/job-scrapper` or update the paths in the service unit. Install uv and make sure `flock` is available on the host. Then run:

```sh
cd ~/apps/job-scrapper
uv sync --locked --no-dev
mkdir -p data ~/.config/systemd/user
cp deploy/job-scrapper.service deploy/job-scrapper.timer ~/.config/systemd/user/
systemctl --user daemon-reload
deploy/run.sh --export-only
systemctl --user enable --now job-scrapper.timer
systemctl --user list-timers job-scrapper.timer
```

Put optional environment overrides in `~/.config/job-scrapper/environment` with mode `600`. Keep the checkout's `data/`, `exports/`, and local configuration when updating the source.

Read console output in `data/service.log` and structured events in `data/job-scrapper.jsonl`. If the timer must run while you are logged out, enable lingering for the deployment user.
