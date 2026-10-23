## Privilege-Escalation Seam

`primitives/privilege.py` is the sole home for "run this as root" (2.3.0-F10 /
STD row 18). Every root-escalating subprocess invocation across the codebase
(pacman, update, provides_lookup, fs_provision, makepkg_invoke,
makepkg_wrapper, kernel, packages, toolchain, reconfigure) routes its argv
through this module, so escalation has a single audit point and one
consistent "am I already root?" decision — no per-callsite `os.geteuid()`
branch, no hand-rolled `["sudo", ...]` list.

### Two entry points

- **`privileged_argv(argv, *, noninteractive=False, mutates=True) -> list[str]`** — builds
  the escalated argv and hands it back; the caller runs it with its own
  `subprocess.run` (or equivalent). Use this at sites that need to stream
  output to the TTY, inspect the return code themselves, or otherwise need
  control over how the command executes. When already root (`euid == 0`) the
  argv is returned unchanged (no `sudo` prefix); `noninteractive=True` inserts
  `-n` so `sudo` fails fast instead of prompting, when not already root.
- **`run_privileged(argv, *, tag, mutates=True, **kwargs) -> subprocess.CompletedProcess`** —
  escalates via `privileged_argv` and executes it through
  `primitives/run.py`'s `run_or_raise` (raise-on-failure). This is the
  convenience for a site that just wants "run this, raise if it fails" with no
  further inspection; `atomic_write`, `artifacts`, `snapshot` and
  `build_sandbox` use it. The sites migrated onto the seam earlier (pacman,
  the kernel install, reconfigure) do **not**: each deliberately retains its
  own `subprocess.run` call to preserve its established error handling and,
  critically, the per-module `subprocess.run` monkeypatch seams the test suite
  relies on. Routing an escalation through `run_privileged` must not replace a
  site whose tests patch that site's own `subprocess.run`.

- **`ensure_credentials() -> bool`** — the one place a password prompt sysforge
  raises itself is made (`3.3.0-F3`). It probes `_credentials_cached()` (`sudo -n
  true`, stdin closed); cached credentials return at once with no yield.
  Otherwise it runs `sudo -v` inside `progress_hooks.hooks().yield_terminal("prompt")`,
  so the progress bar is blanked and an open tracker's clock is paused while the
  operator types, and the wait never reads as build time. `run_privileged` calls it
  first, `sudo_session.authenticate` delegates to it, and the install entry points
  that run real escalations (`pacman.batch_install_pkgs`, the just-in-time install
  inside the `building` tracker, `batch_install_makedeps`, `install_repo_pkgs`,
  `makepkg_wrapper.install_built_packages`) call it right before their
  `subprocess.run`. It is deliberately **not** called from `privileged_argv`:
  that also builds argv a `--dry-run` only prints, and a probe there could prompt
  for a password during a dry run. The suite stubs `_credentials_cached` to
  "cached" by default (`tests/conftest.py::_isolate_sudo_probe`), so no test
  probes the host's sudo. No sudo runs *inside* a build any more (`3.4.0-F1`):
  makepkg's `--install`/`-i` and `--syncdeps`/`-s` are stripped on every path
  (`build_core`'s batch flags, toolchain `build_pass`, the kernel stage, the
  bootstrap `packages` stage, AUR dependency builds) and sysforge does both steps
  itself through this seam. That matters because sudo writes its prompt to the
  controlling terminal (`/dev/tty`), not to the child's output, so a prompt from
  inside a forwarded makepkg could never be seen, paused for or kept off the bar.

This mirrors the streaming/returncode carve-out already established by
`primitives/run.py` for the general subprocess seam (STD row 17):
`run_privileged` is to `privileged_argv` what `run_or_raise` is to a raw
`subprocess.run` call.

### Announcing system changes — `on_mutation`

The seam is also where a change to the system begins, so `privileged_argv`
tells every `on_mutation(listener)` listener of each escalation before
returning its argv (`3.5.0-B138`), with the unescalated argv and even when
already root. The one listener is the verb runner's stage sentinel:
`sentinel_scope(arm_on_mutation=True)` writes its record at the first
announcement rather than on entry, so a verb that fails before changing
anything leaves no recovery record (§CLI Verb Framework). A list of
callables, not a call into `stage_sentinel`, because that module imports this
one.

`mutates=True` is the default, so a new escalation site arms the sentinel
unless it says otherwise. A site passes `mutates=False` only for a command
that cannot leave the system inconsistent if interrupted, each with its reason
beside it:

| Site | Why it is no change to recover from |
|------|-------------------------------------|
| `atomic_write.replace_file` (`install`, `mv`, `rm` of the staged file) | stage + rename is atomic: the old file or the new one |
| `fs_provision._run_priv` | provisioning is idempotent; the next run redoes it |
| `provides_lookup` `pacman -Fy` | a files-database download |
| `build_sandbox`: chroot `install`, `arch-nspawn … pacman -Syu` | the build chroot, not the host |
| `build_sandbox`: `systemctl set-property --runtime` (cap and reset) | runtime-only, reset after the build |
| `build_sandbox` `pacman -Sw` | a download into the package cache |
| `stage_sentinel.Recovery.argv(to_run=False)` (`display`, record validation) | built only to validate or print |

The sandbox's slice reset runs in a `finally` after the build, so without the
marker a failed sandbox build would arm the sentinel on its cleanup, which is
`3.5.0-B138` again.

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

- `authenticate()` — make credentials usable via `privilege.ensure_credentials`
  (probe, then `sudo -v` with inherited stdio and the bar yielded only if sudo
  would prompt) and return whether they are. A no-op returning `True` when already root.
  The return value is the point: a prompt left unanswered until `passwd_timeout`
  fires makes sudo exit non-zero **without ever exec'ing the target command**, so
  a caller can distinguish "not authorized, nothing happened" from "ran and
  failed".
- `keepalive(tag=…, enabled=…)` — a context manager running a daemon thread that
  refreshes credentials every `SUDO_KEEPALIVE_INTERVAL` seconds (60, well under
  the sudoers default `timestamp_timeout` of 5 minutes) for the duration of the
  block, and always stops and joins it on exit including on exception.
  `enabled=False` makes the whole thing a no-op so a dry-run keeps one code path
  at the call site. The refresh is `sudo -v -n` with stdin from `/dev/null`: it
  never prompts, so an expired credential warns instead of putting a password
  prompt under the progress bar and hanging the exit join on it (`3.5.0-B55`).

Why it exists: a stage that builds for hours and then installs with
`sudo pacman -U` from the same process authenticates at stage entry and finds its
timestamp long expired by install time, so an unattended run stops on a prompt
that then goes stale. Three long-building paths use it — the toolchain
stage's four-pass PGO sequence (tag `PGO`), the kernel stage's
build → audit → install window (tag `KERNEL`), and `build_core.build_and_install`
(tag `BUILD`, the engine under `build` and `update`, `3.4.0-F1`), which
authenticates once right before dependency prep and holds the keepalive until it
returns, so AUR dependency installs, just-in-time sibling installs and the final
`pacman -U` never prompt mid-run. Its credential scope is per call
(`_holds_build_credentials`, a context variable carrying the call's `ExitStack`),
so the keepalive is entered at the first point sudo is needed and released on
every return path; a refused or timed-out prompt aborts before anything is built,
and root skips it. `update` holds a second keepalive (tag `UPDATE`, `3.4.0-B2`)
from just before its trailing `pacman -Syu` until the end-of-run sentinel
consume, whose stale boot-entry prune needs sudo when the upgrade touched a
kernel package; the build's keepalive has ended by then and a long `-Syu` (a big
download, a DKMS rebuild) would otherwise leave that prune prompting. The phase
function owns an `ExitStack` that `_hold_credentials` enters only when a `-Syu`
runs. The daemon began life private to
the toolchain stage; a second copy in the kernel stage is exactly the drift the
one-home invariants exist to prevent.

**Callers must `authenticate()` before entering `keepalive()`, and stop when it
returns `False`.** The refresh cannot prompt, so with no cached timestamp it
would only warn every minute while a build runs that can never install.
Authenticating first also puts the one unavoidable prompt at stage entry, while
the operator is still present. The kernel stage, the PGO sequence and `--bolt-only`
each raise before building on a refused or timed-out prompt (`3.5.0-B55`).

The kernel and toolchain stages additionally probe *before* entering
`sentinel_scope`. Their scopes write the record on entry (only the verb
runner's waits for the first change), so any exception inside them leaves
the record in place, so an auth failure raised inside it would demand a recovery
(`mkinitcpio`, or a stock `pacman -S` of the LLVM suite that replaces a working
PGO toolchain) for a mutation that never began. The toolchain's refusal names
`run toolchain --install-only` (PGO) so the built packages install without a
rebuild (`3.5.0-B30`). `--bolt-only` builds nothing, so it never enters the
`PGO` keepalive; `pgo.bolt_only` authenticates before the BOLT and holds its own
(tag `BOLT`) across it, keeping the install's prompt from landing on an
unattended terminal minutes later. The classification stays narrow: only a pre-install
auth failure is a clean abort; a `pacman -U` that actually ran and failed still
leaves the sentinel behind, which is the case it exists for.
