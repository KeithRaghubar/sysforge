## Man Pages

**Current (v2.0) — scdoc hybrid.** `man/sysforge.1` is rendered from a hand-written scdoc template plus auto-generated per-command sections:

```
tools/gen_options.py --template man/sysforge.1.scd.in --out man/sysforge.1.scd
scdoc < man/sysforge.1.scd > man/sysforge.1
```

- **`man/sysforge.1.scd.in`** (committed) — hand-written NAME / SYNOPSIS / DESCRIPTION / GLOBAL OPTIONS / FILES / ENVIRONMENT / EXIT STATUS / EXAMPLES / SEE ALSO / AUTHORS prose, with an `@OPTIONS@` marker where the generated COMMANDS sections splice in. scdoc syntax gotcha: indented continuation lines must not start with a table-control character (`[`, `|`, `]`) — scdoc errors with "Tables cannot be indented".
- **`tools/gen_options.py`** — walks `cli._build_parser()`'s subparser tree depth-first (so `packages add`, `run kernel`, etc. each get a `## <name>` section), emits a synopsis line plus one definition block per positional/option, escapes scdoc formatting characters in help text, and performs the splice itself (no sed). Subparsers registered without `help=` (the internal `completions` data sink) are excluded. Each command section also gets a `*Configuration:*` / `*Environment:*` trailer from the hand-maintained `_VERB_CONFIG` dict at the top of the script (qualified command name → config files / env vars consumed; commands without an entry get no trailer). The FILES and ENVIRONMENT sections of the template carry the inverse index ("Read by: …") — when a verb gains or loses a config source, update both `_VERB_CONFIG` and the template. The trailer is man-page-only by design: it is not wired into argparse epilogs, so `--help` output stays unchanged.
- **`man/sysforge.1.scd`** — intermediate, gitignored. **`man/sysforge.1`** — committed, so AUR-built tarballs ship the page without build-time tooling; the PKGBUILD `package()` installs the committed file directly and needs no man-page makedepend.
- Makefile target: `make man` (pins `COLUMNS=80` so any argparse-derived wrapping is deterministic). `scdoc` is a dev-machine dependency only (installed by `make dev`); `python-argparse-manpage` is no longer used anywhere.
- Release gate: the `manpage` group in `tools/check_shipped.py` reruns the exact same two-step pipeline into temp files and diffs against the committed page (`.TH` date header normalised), so option-help drift in `cli.py` without a `make man` commit blocks the release.

This gives hand-crafted prose with OPTIONS that stay automatically in sync with the CLI — editing flag help in `cli.py` and running `make man` is the whole workflow.

**History:** v0.1.0–v1.x generated the entire page with `argparse-manpage` from the parser tree. The scdoc hybrid (planned since v1.0) replaced it in v2.0.

---
