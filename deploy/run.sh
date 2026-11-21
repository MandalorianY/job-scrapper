#!/usr/bin/env bash
set -euo pipefail
umask 077

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"

environment_file="$HOME/.config/job-scrapper/environment"
if [[ -f "$environment_file" ]]; then
    set -a
    # shellcheck source=/dev/null
    source "$environment_file"
    set +a
fi

mkdir -p data exports
exec 9>data/.run.lock
if ! flock --nonblock 9; then
    echo "A scraper run is already active; skipping this run."
    exit 0
fi

exec .venv/bin/job-scrapper "$@"
