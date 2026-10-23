#!/usr/bin/env bash
# run.sh -- the end-to-end tier (3.5.0-DEV21): the documented paths run on a
# fresh Arch system as an unprivileged user, asserting on exit codes, pacman,
# the state files and leftover sentinels.
#
# Usage: run.sh [SCENARIO...]          each scenario in its own podman container
#        run.sh --here [SCENARIO...]   on this system, as root (CI: a fresh
#                                      archlinux:base-devel container)
#
# SCENARIO is a name under tools/e2e/scenarios/ (`quickstart` or
# `10-quickstart.sh`); default all, in order. sysforge is built from HEAD, or
# E2E_COMMIT (any revision: an older one to bisect a regression with today's
# scenarios); uncommitted edits are not in it. The scenarios run from the
# working tree.
#
# A scenario's header says where it starts: `# start: installed` (sysforge
# just installed, Quick start step 1) or `# start: ready` (the one-time setup
# done too). `# xfail: <ID>` marks a scenario that fails until that roadmap
# item is fixed: its failure reports XFAIL, and a pass fails the run so the
# marker is removed with the fix.
set -euo pipefail

repo=$(cd "$(dirname "$0")/../.." && pwd)
here=false
if [[ ${1:-} == --here ]]; then
    here=true
    shift
fi

scenarios=()
if [[ $# -gt 0 ]]; then
    for name in "$@"; do
        match=("$repo"/tools/e2e/scenarios/*"${name%.sh}"*.sh)
        [[ -f ${match[0]} ]] || { echo "e2e: no scenario matches $name" >&2; exit 2; }
        scenarios+=("${match[0]}")
    done
else
    scenarios=("$repo"/tools/e2e/scenarios/*.sh)
fi

header() {  # header FILE KEY: the value of the scenario's `# KEY: value` line
    sed -n "s/^# $2: *//p" "$1" | head -1
}

# CI's checkout belongs to another user than the one reading it; git refuses
# such a repository unless it is marked safe (the demo user's clone too).
if $here; then
    [[ $EUID == 0 ]] || { echo "e2e: --here runs as root (it provisions the system)" >&2; exit 2; }
    git config --system --add safe.directory "$repo"
    git config --system --add safe.directory /src/sysforge
fi
commit=$(git -C "$repo" rev-parse "${E2E_COMMIT:-HEAD}^{commit}")
if [[ -z ${E2E_COMMIT:-} ]] && [[ -n $(git -C "$repo" status --porcelain -- sysforge etc PKGBUILD-git sysforge.install pyproject.toml) ]]; then
    echo "e2e: note: uncommitted changes to sysforge are not in the build (it builds HEAD)" >&2
fi

# run_one FILE: run a scenario in its start state; return its exit status.
if $here; then
    # The demo scripts read the checkout at /src/sysforge, as in the container.
    if [[ $repo != /src/sysforge ]]; then
        mkdir -p /src
        rm -rf /src/sysforge
        cp -a "$repo" /src/sysforge
    fi
    bash /src/sysforge/tools/demo/provision.sh
    runuser -l demo -c "/src/sysforge/tools/demo/install.sh $commit"
    set_up=false
    run_one() {
        # One system for every scenario: the first `ready` one after no
        # Quick start runs the setup the Quick start would have.
        if [[ $(header "$1" start) == ready ]] && ! $set_up; then
            runuser -l demo -c 'sudo sysforge run hardware </dev/null && sudo EDITOR=true sysforge run reconfigure </dev/null'
        fi
        set_up=true
        runuser -l demo -c "bash /src/sysforge/tools/e2e/scenario.sh /src/sysforge/tools/e2e/scenarios/$(basename "$1")"
    }
else
    command -v podman >/dev/null || { echo "e2e: podman is not installed (sudo pacman -S podman)" >&2; exit 1; }
    base=sysforge-demo:base
    prep=sysforge-e2e-prep
    cleanup() { podman rm -f -t 0 "$prep" sysforge-e2e-run >/dev/null 2>&1 || true; }
    trap cleanup EXIT
    cd "$repo"
    echo "==> Building $base"
    podman build -q -t "$base" -f tools/demo/Containerfile tools/demo >/dev/null
    echo "==> Installing sysforge ($commit)"
    cleanup
    podman run -d --name "$prep" -v "$repo:/src/sysforge:ro" "$base" >/dev/null
    podman exec -u demo -w /home/demo "$prep" /src/sysforge/tools/demo/install.sh "$commit" >/dev/null
    podman commit -q "$prep" sysforge-e2e:installed >/dev/null
    echo "==> Running the one-time setup"
    podman exec -u demo -w /home/demo "$prep" \
        bash -c 'sudo sysforge run hardware </dev/null && sudo EDITOR=true sysforge run reconfigure </dev/null' >/dev/null
    podman commit -q "$prep" sysforge-e2e:ready >/dev/null
    podman rm -f -t 0 "$prep" >/dev/null
    run_one() {
        local rc=0
        podman run -d --name sysforge-e2e-run -v "$repo:/src/sysforge:ro" \
            "sysforge-e2e:$(header "$1" start)" >/dev/null
        podman exec -u demo -w /home/demo sysforge-e2e-run \
            bash /src/sysforge/tools/e2e/scenario.sh "/src/sysforge/tools/e2e/scenarios/$(basename "$1")" || rc=$?
        podman rm -f -t 0 sysforge-e2e-run >/dev/null
        return "$rc"
    }
fi

results=()
failed=0
for file in "${scenarios[@]}"; do
    name=$(basename "$file" .sh)
    xfail=$(header "$file" xfail)
    echo "==> $name"
    if run_one "$file"; then
        if [[ -n $xfail ]]; then
            results+=("XPASS  $name (passes: remove its xfail: $xfail marker)")
            failed=1
        else
            results+=("PASS   $name")
        fi
    elif [[ -n $xfail ]]; then
        results+=("XFAIL  $name ($xfail)")
    else
        results+=("FAIL   $name")
        failed=1
    fi
done

echo
echo "== e2e results"
printf '%s\n' "${results[@]}"
exit "$failed"
