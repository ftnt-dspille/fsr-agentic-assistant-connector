#!/usr/bin/env bash
# Put the wheel for one EXACT pin (name==version) into <dest>, downloading it from the index only
# when this machine has not fetched that pin before.
#
#   fetch_pinned_wheel.sh <name==version> <dest-dir> <cache-dir>
#
# The cache holds one directory per exact pin: <cache-dir>/<name>-<version>/<wheel>. A published
# version is immutable, so a cached wheel for the pin is the wheel the index would serve; a different
# version misses the cache and downloads. The interpreter is $PIP (default python3); it must have pip.
# Exit 0 when the wheel is in <dest>; non-zero (nothing cached) when the download fails.
set -euo pipefail

if [[ $# -ne 3 ]]; then
    echo "usage: $0 <name==version> <dest-dir> <cache-dir>" >&2
    exit 2
fi
pin="$1"; dest="$2"; cache="$3"
if [[ ! "$pin" =~ ^([A-Za-z0-9][A-Za-z0-9._-]*)==([A-Za-z0-9][A-Za-z0-9._+!-]*)$ ]]; then
    echo "fetch_pinned_wheel: not an exact name==version pin: $pin" >&2
    exit 2
fi
name="${BASH_REMATCH[1]}"; ver="${BASH_REMATCH[2]}"
wheel_prefix="${name//-/_}-${ver}-"          # PEP 427 file-name prefix, e.g. fsr_playbooks-0.4.50-
pin_dir="$cache/${name}-${ver}"

mkdir -p "$dest"
shopt -s nullglob
cached=( "$pin_dir"/"$wheel_prefix"*.whl )
if (( ${#cached[@]} )); then
    cp "${cached[0]}" "$dest/"
    echo "   cached: $(basename "${cached[0]}") (pin $pin, no download)"
    exit 0
fi

"${PIP:-python3}" -m pip download --no-deps --only-binary=:all: -d "$dest" "$pin"

fetched=( "$dest"/"$wheel_prefix"*.whl )
if (( ${#fetched[@]} == 0 )); then
    echo "fetch_pinned_wheel: pip fetched nothing matching ${wheel_prefix}*.whl for $pin" >&2
    exit 1
fi
mkdir -p "$pin_dir"
for w in "${fetched[@]}"; do cp "$w" "$pin_dir/"; done
