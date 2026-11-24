#!/usr/bin/env bash
set -euo pipefail

tap=$1
version=$2
checksum=$3
templates=$(dirname -- "${BASH_SOURCE[0]}")

mkdir -p "$tap/Formula"
sed -e "s/@VERSION@/$version/g" -e "s/@SHA256@/$checksum/g" \
    "$templates/jobsfr.rb.in" > "$tap/Formula/jobsfr.rb"

cp "$templates/tap-readme.md" "$tap/README.md"
git -C "$tap" add Formula/jobsfr.rb README.md

renames="$tap/formula_renames.json"
if [[ -f "$renames" ]]; then
    jq 'with_entries(select(.value != "jobsfr"))' "$renames" > "$renames.tmp"
    mv -- "$renames.tmp" "$renames"
    git -C "$tap" add formula_renames.json
fi
