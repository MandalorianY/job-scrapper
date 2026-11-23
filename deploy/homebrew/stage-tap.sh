#!/usr/bin/env bash
set -euo pipefail

tap=$1
version=$2
checksum=$3
templates=$(dirname -- "${BASH_SOURCE[0]}")
renames="$tap/formula_renames.json"

mkdir -p "$tap/Formula"
sed -e "s/@VERSION@/$version/g" -e "s/@SHA256@/$checksum/g" \
    "$templates/jobsfr.rb.in" > "$tap/Formula/jobsfr.rb"

if [[ ! -f "$renames" ]]; then
    printf '{}\n' > "$renames"
fi
jq --sort-keys '
    def flatten: . as $renames | map_values(if $renames[.] == "jobsfr" then "jobsfr" else . end);
    .["job-scrapper"] = "jobsfr" | until(. == flatten; flatten)
' "$renames" > "$renames.tmp"
mv -- "$renames.tmp" "$renames"

if [[ -f "$tap/Formula/job-scrapper.rb" ]]; then
    cp "$templates/tap-readme.md" "$tap/README.md"
    git -C "$tap" rm -- Formula/job-scrapper.rb
    git -C "$tap" add README.md
fi
git -C "$tap" add Formula/jobsfr.rb formula_renames.json
