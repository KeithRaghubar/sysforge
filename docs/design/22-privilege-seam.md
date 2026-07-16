## Privilege-Escalation Seam

`primitives/privilege.py` is the sole home for "run this as root" (2.3.0-F10 /
STD row 18). Every root-escalating subprocess invocation across the codebase
(pacman, update, provides_lookup, fs_provision, makepkg_invoke,
makepkg_wrapper, kernel, packages, toolchain, reconfigure) routes its argv
through this module, so escalation has a single audit point and one
consistent "am I already root?" decision — no per-callsite `os.geteuid()`
branch, no hand-rolled `["sudo", ...]` list.

### Two entry points

- **`privileged_argv(argv, *, noninteractive=False) -> list[str]`** — builds
  the escalated argv and hands it back; the caller runs it with its own
  `subprocess.run` (or equivalent). Use this at sites that need to stream
  output to the TTY, inspect the return code themselves, or otherwise need
  control over how the command executes. When already root (`euid == 0`) the
  argv is returned unchanged (no `sudo` prefix); `noninteractive=True` inserts
  `-n` so `sudo` fails fast instead of prompting, when not already root.
- **`run_privileged(argv, *, tag, **kwargs) -> subprocess.CompletedProcess`** —
  escalates via `privileged_argv` and executes it through
  `primitives/run.py`'s `run_or_raise` (raise-on-failure). This is the
  available convenience for a new site that just wants "run this, raise if it
  fails" with no further inspection. Note that the migrated sites do **not**
  use it: each deliberately retains its own `subprocess.run` call to preserve
  its established error handling and — critically — the per-module
  `subprocess.run` monkeypatch seams the test suite relies on. `run_privileged`
  therefore currently has no callers; it exists as the sanctioned raise-on-failure
  path for future code, and routing an escalation through it must not replace a
  site whose tests patch that site's own `subprocess.run`.

This mirrors the streaming/returncode carve-out already established by
`primitives/run.py` for the general subprocess seam (STD row 17):
`run_privileged` is to `privileged_argv` what `run_or_raise` is to a raw
`subprocess.run` call.

### Escalate / probe / drop-priv taxonomy

Not every `sudo`-prefixed argv is an escalation, and the seam only owns the
escalation case:

- **Escalate** — an operation genuinely needs root (installing packages,
  writing to `/etc`, provisioning directories). This is what
  `privileged_argv`/`run_privileged` are for; every such site must route
  through them.
- **Probe** — checking or refreshing sudo credentials without running a
  privileged command: `sudo -v` (credential refresh) and `sudo -n true`
  (non-interactive "am I already authenticated" check). These aren't
  escalating anything and stay as raw calls.
- **Drop-privilege** — `sudo -u <user> ...` runs a command as a *specific
  non-root* user (e.g. building a package as an unprivileged build user from
  a root-invoked entry point). This is the inverse of escalation and also
  stays as a raw call.

The `check_standards` `privilege_seam` group (`tools/check_standards.py`)
enforces the boundary: it walks every `sysforge/*.py` file (except
`primitives/privilege.py` itself, the sanctioned home) for AST list literals
whose first element is the string constant `"sudo"`, and flags them as an
error **unless** the argv tail structurally matches one of the allowlisted
non-escalation forms above (`-v`; `-n true`; `-u <any>`). A raw `["sudo",
"pacman", "-Syu"]` outside `privilege.py` fails the gate; `["sudo", "-v"]` and
`["sudo", "-u", user, ...]` do not. See `tests/test_standards_compliance.py`
for both the fixture-based checker tests and the `privileged_argv` behaviour
test (root passthrough vs. non-root `sudo`-prefixing).

### Polkit non-goal

Polkit/`pkexec` was evaluated as an alternative escalation mechanism and
declined for this tool's execution model: SysForge's privileged operations
run interactively from a TTY (build/update/setup sessions), where `sudo`'s
credential caching and terminal-native prompt fit naturally, whereas `pkexec`
targets GUI-mediated one-shot authorization and would add a second prompt
mechanism without a matching need. The seam is deliberately the single
insertion point for escalation — if a polkit-based mechanism became
warranted later (e.g. a GUI front-end), `privileged_argv`/`run_privileged`
are where that swap would happen, without touching call sites.
