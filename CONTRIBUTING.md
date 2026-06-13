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

- `make test` and `make lint` must pass. Use the Makefile targets, not direct
  `pytest`/`ruff` invocations.
- **`DESIGN.md` is generated** — edit the sources under `docs/design/*.md`, then run
  `make design`. Never edit `DESIGN.md` directly. Doc update order: `docs/design/*.md`
  → README.md → CLAUDE.md.
- CLI surface changes (new verb, flag, help text) must update
  `completions/_sysforge` + `completions/sysforge.bash` and regenerate the man page
  (`make man`) **in the same change**. `make check-shipped` verifies this.
- New logic that branches on the resolved compiler (gcc vs llvm/clang) ships with
  tests for **both** paths.
- Keep behavior changes and refactors in separate commits where practical.

## Getting started

```bash
git clone https://github.com/KeithRaghubar/sysforge.git
cd sysforge
uv sync          # create the venv and install dev deps
make test        # full suite
```

## License

By contributing you agree your work is licensed under the project's MIT license.
