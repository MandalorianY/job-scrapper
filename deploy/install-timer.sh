#!/usr/bin/env bash
set -euo pipefail

if [[ $(uname -s) != Linux ]]; then
    echo "On macOS, install the Homebrew formula and run jobsfr-install-timer." >&2
    exit 1
fi

launcher=$(realpath -- "${BASH_SOURCE[0]}")
checkout=$(cd -- "$(dirname -- "$launcher")/.." && pwd -P)
exec uv run --project "$checkout" --locked --no-dev jobsfr-install-timer "$@"
