#!/usr/bin/env bash
# record-demo.sh -- re-record the terminal recordings under docs/assets/.
#
# Usage: record-demo.sh [TAPE...]   (default: every docs/assets/*.tape)
#
# Each tape runs real commands in a fresh Arch container (tools/demo/), set up
# to the end of the Quick start's step 3: sysforge built from this branch's
# committed HEAD and the one-time setup done. That setup runs once and is saved
# as an image; every tape then gets its own container from it, so no recording
# depends on another or on this machine's packages, config or paths.
set -euo pipefail

repo=$(cd "$(dirname "$0")/.." && pwd)
base=sysforge-demo:base
ready=sysforge-demo:ready

for tool in vhs podman; do
    if ! command -v "$tool" >/dev/null; then
        echo "record-demo: $tool is not installed (sudo pacman -S $tool)" >&2
        exit 1
    fi
done

cd "$repo"
if [[ $# -gt 0 ]]; then
    tapes=("$@")
else
    tapes=(docs/assets/*.tape)
fi
for tape in "${tapes[@]}"; do
    vhs validate "$tape" >/dev/null || { vhs validate "$tape"; exit 1; }
done
branch=$(git branch --show-current)
if [[ -n $(git status --porcelain) ]]; then
    echo "record-demo: note: uncommitted changes are not in the recording (it builds $branch's HEAD)" >&2
fi

echo "==> Building $base"
podman build -q -t "$base" -f tools/demo/Containerfile tools/demo >/dev/null

echo "==> Installing sysforge ($branch) and running the one-time setup"
podman rm -f -t 0 sysforge-demo-prep >/dev/null 2>&1 || true
podman run -d --name sysforge-demo-prep -v "$repo:/src/sysforge:ro" "$base" >/dev/null
trap 'podman rm -f -t 0 sysforge-demo-prep >/dev/null 2>&1 || true' EXIT
podman exec -u demo -w /home/demo sysforge-demo-prep /src/sysforge/tools/demo/prepare.sh "$branch" \
    >/dev/null
podman commit -q sysforge-demo-prep "$ready" >/dev/null
podman rm -f -t 0 sysforge-demo-prep >/dev/null

for tape in "${tapes[@]}"; do
    name=sysforge-demo-$(basename "$tape" .tape)
    echo "==> Recording $tape"
    podman rm -f -t 0 "$name" >/dev/null 2>&1 || true
    podman run -d --name "$name" "$ready" >/dev/null
    DEMO_CONTAINER=$name vhs "$tape"
    podman rm -f -t 0 "$name" >/dev/null
done
