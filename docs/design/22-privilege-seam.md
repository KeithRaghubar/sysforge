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
- **Self-escalating tool** — a tool that escalates *itself* and must be
  invoked unescalated. `makechrootpkg` (the build sandbox, §Makepkg Wrapper)
  re-execs under `sudo` via devtools' `check_root` with its own
  env-preserve list; prefixing our own `sudo` would strip
  `PKGDEST`/`SRCDEST`/`LOGDEST`/`MAKEFLAGS` and silently relocate every
  built artifact. It is invoked as a bare argv and so never reaches the
  checker, which only sees literal `"sudo"` heads — the exemption is
  documented here rather than allowlisted because there is nothing to
  allowlist.

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

### Credential lifetime — `primitives/sudo_session.py`

Escalation and credential *lifetime* are orthogonal concerns and have separate
homes. `privilege.py` answers "how does this argv run as root"; it explicitly
puts auth probes out of scope. `primitives/sudo_session.py` answers the two
questions left over — "how long does an already-granted credential stay valid"
and "is it valid right now" — and is the sole home for both. Everything it does
is expressed through `sudo -v`, which escalates nothing and is structurally
allowlisted by the `privilege_seam` checker anywhere in the tree.

Two entry points:

- `authenticate()` — run the `sudo -v` probe with inherited stdio and return
  whether credentials are usable. A no-op returning `True` when already root.
  The return value is the point: a prompt left unanswered until `passwd_timeout`
  fires makes sudo exit non-zero **without ever exec'ing the target command**, so
  a caller can distinguish "not authorized, nothing happened" from "ran and
  failed".
- `keepalive(tag=…, enabled=…)` — a context manager running a daemon thread that
  refreshes credentials every `SUDO_KEEPALIVE_INTERVAL` seconds (60, well under
  the sudoers default `timestamp_timeout` of 5 minutes) for the duration of the
  block, and always stops and joins it on exit including on exception.
  `enabled=False` makes the whole thing a no-op so a dry-run keeps one code path
  at the call site.

Why it exists: a stage that builds for hours and then installs with
`sudo pacman -U` from the same process authenticates at stage entry and finds its
timestamp long expired by install time, so an unattended run stops on a prompt
that then goes stale. Both long-building stages now use it — the toolchain
stage's four-pass PGO sequence (tag `PGO`) and the kernel stage's
build → audit → install window (tag `KERNEL`). The daemon began life private to
the toolchain stage; a second copy in the kernel stage is exactly the drift the
one-home invariants exist to prevent.

**Callers must `authenticate()` before entering `keepalive()`.** The refresh
inherits stdio, so with no cached timestamp it would prompt from a background
thread, interleaving with build output. Authenticating first also puts the one
unavoidable prompt at stage entry, while the operator is still present.

The kernel stage additionally probes *before* entering `sentinel_scope`. By the
sentinel contract any exception inside the scope leaves the record in place, so
an auth failure raised inside it would demand a recovery `mkinitcpio` for a
mutation that never began. The classification stays narrow: only a pre-install
auth failure is a clean abort; a `pacman -U` that actually ran and failed still
leaves the sentinel behind, which is the case it exists for.
