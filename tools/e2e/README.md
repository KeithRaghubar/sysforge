# SysForge End-to-End Tier

The documented paths, run the way a new user meets them: on a fresh Arch system, as an
unprivileged user, one command after another. The unit suite stubs out exactly the places this
tier exercises: root versus the user, group membership that applies only at the next login, the
state one command leaves for the next, and a real terminal.

```bash
make e2e                          # every scenario, each in its own container (needs podman)
make e2e SCENARIO="drift optin"   # only the named ones
make e2e E2E_HERE=1               # as root on this system, no podman: what CI runs
```

sysforge is built from the checkout's committed `HEAD`. Uncommitted edits under `sysforge/` are
not in the build; edits to the scenarios are, since they run from the working tree. To find when a
scenario broke, build an older revision with today's scenarios:
`make e2e E2E_COMMIT=<rev> SCENARIO=quickstart`.

## How it runs

`run.sh` builds the demo image (`tools/demo/`, shared with `make demo-gif`), installs sysforge in
it as the `demo` user (`tools/demo/install.sh`) and saves two start states:

| Start state | What is done | Scenarios |
|-------------|--------------|-----------|
| `installed` | Quick start step 1: the package installed | the Quick start itself |
| `ready` | the one-time setup too (`run reconfigure`, `run hardware`) | everything else |

Each scenario then gets a fresh container from its start state and runs in one `podman exec`,
that is one login session. With `--here` every scenario shares the one system in order, and the
first `ready` scenario runs the setup itself when the Quick start did not.

The scenarios assert on exit codes, `pacman -Qi`, `build_state.toml`, `packages.toml` and leftover
sentinels (`lib.sh`), not on rendered output. Prompts are answered on a pseudo-terminal
(`answer.py`), so `build` takes the same interactive paths it takes at a real terminal.

## Scenarios

| Scenario | Covers |
|----------|--------|
| `10-quickstart` | the README Quick start in one login: setup, an AUR build through the review gate, `update` |
| `20-repo-optin` | a repo package refused without a terminal, the opt-in prompt, the saved answer |
| `30-flag-drift` | a default-profile change reported by `update --dry-run` and rebuilt by `--rebuild-on-flag-drift` |
| `40-pre-install-failure` | a build failing before any install leaves no sentinel |

Packages come from `pkgs/`, local PKGBUILDs with no network sources, so an outage can't flake the
tier. `10-quickstart` is the one networked scenario (the AUR, and pacman for build dependencies).
`pkgs/tree` is a stand-in named after a repo package, which puts `build` on its repo opt-in path
without fetching Arch's packaging repo.

## Writing a scenario

A scenario is a bash fragment in `scenarios/`, sourced with `set -euo pipefail` after `lib.sh`,
running as `demo` in `~`. Its header names where it starts, and optionally a roadmap item it
fails until fixed:

```bash
# What the scenario shows, in a sentence or two.
# start: ready
# xfail: <roadmap ID>
```

An `xfail` scenario's failure reports `XFAIL` and keeps the run green; once it passes it reports
`XPASS` and fails the run, so the marker comes off in the fix's own change. Keep it local: build
from `stage_pkg <name>`, not a networked package, unless the network is the point.
