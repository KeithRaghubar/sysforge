# Contributing to SysForge

Thanks for your interest. Issues and pull requests are both welcome.

## How this project is developed

SysForge is developed essentially end-to-end with Claude Code, with the maintainer
reviewing every change. Contributions are reviewed the same way: the maintainer (often
with AI assistance) reads, tests, and may rework your change before merging. If a PR is
substantially reshaped, you keep authorship credit.

If you'd rather not write code, a well-described issue (repro steps, config, logs) is
just as valuable — many features here started as workflow pain points.

## Ground rules for PRs

Changes reach `main` by pull request, and the CI `gates` job is a required check: it runs
every gate in a clean Arch container, and a red run blocks the merge. Run the same set
locally with `make pre-release`, or reproduce CI exactly with `make ci-local` (podman).
`make install-hooks` adds a pre-commit hook running the fast gates, so most failures show
up before you push.

- Use the Makefile targets, not direct `pytest`/`ruff` invocations.
- **`DESIGN.md` is generated**: edit the sources under `docs/design/*.md`, then run
  `make design`. Never edit `DESIGN.md` directly. Doc update order: `docs/design/*.md`
  → README.md → CLAUDE.md.
- New logic that branches on the resolved compiler (gcc vs llvm/clang) ships with
  tests for **both** paths.
- Keep behavior changes and refactors in separate commits where practical.

### Changing anything a user can see

A flag, command, config key, environment variable, `--json` field or log name is a
*surface*. Change one and, in the same PR:

| Also update | Enforced by |
|---|---|
| Both completion scripts (`completions/_sysforge`, `completions/sysforge.bash`) | `make check-shipped` (`completion_commands`) |
| The man page: `make man` (generated from the parser and the env/journal tables) | `make check-shipped` (`manpage`) |
| `tests/data/surface.json`: `make surface`; its diff is what a reviewer reads | `make check-surface` |
| A release-note entry in `docs/release-notes/unreleased.md` naming what changed | `make check-surface`, `make check-standards` |
| **Removing or renaming** a name: add the old one to `tools/retired_names.toml` (a config key: `RETIRED_KEYS` in `sysforge/primitives/config_schema.py`) | `make check-surface`, `make check-standards` (`retired_names`) |
| A **new config key**: document it in the shipped `etc/sysforge/*.toml` (and its twin under `tests/data/etc/sysforge/`) | `tests/test_config_keys_documented.py`, `make check-shipped` |
| A **new environment variable**: a row in `sysforge/primitives/env_vars.py` | `tests/test_env_vars.py` |
| A **new `--json` field**: its schema under `tests/data/json-schema/` | `tests/test_json_output.py` |
| The user guide for the feature (`docs/guides/`) | review only |

In tests, build the real objects: `BuildOptions(...)`, `RunOptions(...)`, or
`cli_args("state", "list", ...)` from `tests/cli_args.py` for a parsed command line.
`SimpleNamespace` stand-ins drift from the real thing (`tests/test_stand_ins.py`).

## Getting started

```bash
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
uv sync          # create the venv and install dev deps
make test        # full suite
```

## License

By contributing you agree your work is licensed under the project's MIT license.
