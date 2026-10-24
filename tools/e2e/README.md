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
| `50-maintain` | `state list`, `resolve`, `log`, `packages add`/`list`/`remove`/`add-group`, `state forget` and `uninstall` on a source-built package |
| `60-revert-to-stock` | a source-built repo package replaced by the stock one, its record dropped |
| `70-config` | `config check`, `clean` and `reset`, and `setup --preset` undone by `config reset` |
| `80-inspect` | the read-only commands (`state` reports, `doctor`, `env`, `fetch`) change nothing |
| `85-artifacts` | a user script discovered, adopted, reviewed, edited, deployed and removed |
| `90-source-policy` | `[aur] enabled = false` and `freeze_sources` refuse before any checkout; `search` with the AUR off |
| `95-stage-dry-run` | `run toolchain`/`run kernel --dry-run` with non-default configs change nothing, and the kernel's shows its plan |

Packages come from `pkgs/`, local PKGBUILDs with no network sources, so an outage can't flake the
tier. Three scenarios need the network, because it is the point: `10-quickstart` (the AUR, and
pacman for build dependencies), `60-revert-to-stock` (the stock package from the mirrors) and
`90-source-policy` (the AUR lookup a frozen build makes before refusing the clone).
`pkgs/tree` is a stand-in named after a repo package, which puts `build` on its repo opt-in path
without fetching Arch's packaging repo.

## Coverage

Every command in `tests/data/surface.json` is either run by a scenario or listed in
`uncovered.toml` with the reason none can run it (a disk install, a multi-hour build, a
bootloader the container lacks). `tests/test_e2e_coverage.py` fails the suite on a command in
neither, and on a listing a scenario has since made redundant, so a new command cannot go
untested unnoticed. `make e2e-coverage` prints both sets. A command counts once a scenario line
runs it (`sysforge <command>`, also inside `answer`, `expect_exit` or `output_has`), however
little it checks: the check finds a missing scenario, not a weak one.

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
