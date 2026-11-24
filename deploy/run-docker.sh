#!/usr/bin/env bash
set -euo pipefail

caller_dir=$(pwd -P)
launcher=$(realpath -- "${BASH_SOURCE[0]}")
cd -- "$(dirname -- "$launcher")/.."
checkout=$(pwd -P)
mkdir -p data exports

mounts=()
profile_host_path=
host_dirs=("$(realpath data)" "$(realpath exports)")
container_dirs=(/work/data /work/exports)

bind_mount() {
    local source=${1//\"/\"\"} target=${2//\"/\"\"}
    mounts+=(--mount "type=bind,\"src=$source\",\"dst=$target\"${3:+,$3}")
}

# Keep one container path for a file already reachable through a writable mount.
# In particular, DuckDB compares the replay archive and destination by path.
use_mounted_path() {
    local index
    for index in "${!host_dirs[@]}"; do
        if [[ $1 == "${host_dirs[index]}" || $1 == "${host_dirs[index]}/"* ]]; then
            mapped_path=${container_dirs[index]}${1#"${host_dirs[index]}"}
            return 0
        fi
    done
    return 1
}

map_output() {
    local path=$1 option=$2 directory filename=
    if [[ $option == --export-dir ]]; then
        directory=$path
    else
        if [[ -e $path ]]; then
            path=$(realpath -- "$path")
        fi
        directory=$(dirname -- "$path")
        filename=/$(basename -- "$path")
    fi
    mkdir -p -- "$directory"
    directory=$(realpath -- "$directory")
    if ! use_mounted_path "$directory"; then
        mapped_path=/work/.output-${#host_dirs[@]}
        host_dirs+=("$directory")
        container_dirs+=("$mapped_path")
        bind_mount "$directory" "$mapped_path"
    fi
    mapped_path+=$filename
}

map_input() {
    local path=$1 option=$2 index=$3 target companion
    if [[ $path != /* && -f $caller_dir/$path ]]; then
        path=$caller_dir/$path
    fi
    if [[ ! -f $path ]]; then
        printf '%s: file not found: %s\n' "$option" "$path" >&2
        exit 2
    fi
    path=$(realpath -- "$path")
    if [[ $option == --archive ]] && use_mounted_path "$path"; then
        return
    fi
    target=/work/.input-$index
    mapped_path=$target/$(basename -- "$path")
    bind_mount "$path" "$mapped_path" readonly
    if [[ $option == --profile ]]; then
        profile_host_path=$path
    fi
    if [[ $option == --archive ]]; then
        for companion in "$path.wal" "$path".location-scope-*.json "$(dirname -- "$path")/.geography"; do
            if [[ -e $companion ]]; then
                bind_mount "$companion" "$target/$(basename -- "$companion")" readonly
            fi
        done
    fi
}

bind_mount "${host_dirs[0]}" "${container_dirs[0]}"
bind_mount "${host_dirs[1]}" "${container_dirs[1]}"
if [[ -f .env ]]; then
    bind_mount "$checkout/.env" /work/.env readonly
fi
local_config=src/jobsfr/config/config.local.yaml
if [[ -f "$local_config" ]]; then
    bind_mount "$checkout/$local_config" /work/config.local.yaml readonly
fi

# Register writable destinations before mapping replay inputs, regardless of CLI argument order.
args=("$@")
for phase in output input; do
    for ((i = 0; i < $#; i++)); do
        option=${args[i]%%=*}
        case $option in
            --db|--log-file|--export-dir) kind=output ;;
            --profile|--archive) kind=input ;;
            --query|-q|--location|-l|--results-wanted|--custom-pages|--log-level)
                if [[ ${args[i]} != *=* ]]; then
                    i=$((i + 1))
                fi
                continue
                ;;
            --) break ;;
            *) continue ;;
        esac
        prefix=
        if [[ ${args[i]} == *=* ]]; then
            prefix=$option=
            path=${args[i]#*=}
        elif ((i + 1 < $#)) && [[ ${args[i + 1]} != -* ]]; then
            i=$((i + 1))
            path=${args[i]}
        else
            printf '%s requires a path.\n' "$option" >&2
            exit 2
        fi
        [[ $kind == "$phase" && -n $path ]] || continue
        case $path in
            \~/*) path=$HOME/${path:2} ;;
            /work/data|/work/data/*|/work/exports|/work/exports/*) path=$checkout/${path#/work/} ;;
        esac
        if [[ $kind == output ]]; then
            map_output "$path" "$option"
        else
            map_input "$path" "$option" "$i"
        fi
        args[i]=$prefix$mapped_path
    done
done

if [[ -n $profile_host_path ]]; then
    mounts+=(--env "JOBSFR_PROFILE_HOST_PATH=$profile_host_path")
fi
image=$(docker build --quiet --tag jobsfr:local .)
exec docker run --rm --init --read-only --cap-drop ALL \
    --security-opt no-new-privileges=true --user "$(id -u):$(id -g)" \
    --tmpfs /tmp:rw,noexec,nosuid,nodev --env JOBSFR_DISABLE_LOCAL_CONFIG \
    "${mounts[@]}" "$image" ${args[@]+"${args[@]}"}
