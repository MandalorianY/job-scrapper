#!/usr/bin/env bash
set -euo pipefail

launcher=$(realpath -- "${BASH_SOURCE[0]}")
cd -- "$(dirname -- "$launcher")/.."
mkdir -p data exports

mounts=(
    --mount "type=bind,src=$PWD/data,dst=/work/data"
    --mount "type=bind,src=$PWD/exports,dst=/work/exports"
)
if [[ -f .env ]]; then
    mounts+=(--mount "type=bind,src=$PWD/.env,dst=/work/.env,readonly")
fi
local_config=src/scrapper/config/config.local.yaml
if [[ -f "$local_config" ]]; then
    mounts+=(--mount "type=bind,src=$PWD/$local_config,dst=/opt/venv/lib/python3.14/site-packages/scrapper/config/config.local.yaml,readonly")
fi

image=$(docker build --quiet --tag job-scrapper:local .)
exec docker run --rm --init --read-only --cap-drop ALL \
    --security-opt no-new-privileges=true --user "$(id -u):$(id -g)" \
    --tmpfs /tmp:rw,noexec,nosuid,nodev \
    "${mounts[@]}" "$image" "$@"
