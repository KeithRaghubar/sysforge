<!--
SPDX-FileCopyrightText: 2026 Keith Raghubar

SPDX-License-Identifier: MIT
-->

# Frozen sources — a hard gate on AUR/VCS downloads

**Roadmap ID:** `3.0.0-F2`
**Date:** 2026-08-09
**Status:** design approved, not implemented

## Motivation

Recent AUR malware incidents shipped hostile code through package sources. SysForge's
existing controls do not cover that case:

- `--offline` (`sysforge/cli.py:403`) *skips* network work as a convenience. It is
  threaded per-callsite (`update.py:588`, `update_sync.py:72`, `llvm_state.py:294`) and
  is unknown to the egress primitives themselves — `aur.py`'s `urlopen` sites and
  `vcs_pkgver.py:248`'s `git ls-remote` never consult it. Any code path that forgets the
  check reaches the network.
- The PKGBUILD review gate (`primitives/pkgbuild_review.py`) is the existing supply-chain
  control, but it auto-accepts on non-TTY runs and `update` passes `interactive=False` by
  default — silent in exactly the unattended case where malware lands.
- Neither control sits early enough for `--devel`. `update_version.py:263` calls
  `evaluate_vcs_pkgver`, whose `makepkg -od` invocation (`vcs_pkgver.py:42`) **sources the
  PKGBUILD and runs `pkgver()`**. `--nobuild` suppresses `build()`, not top-level statements,
  and running `pkgver()` is the invocation's entire purpose. Hostile code therefore executes
  as the build user during the *version check* — upstream of the build, and so upstream of
  every gate placed at the build.

This feature adds a **source freeze**: an enforced denial of code ingress. Existing
checkouts still build; nothing new arrives without a deliberate, per-run, per-package
lift.

## Scope

**Gated** (code, or pointers to code sysforge will execute):

1. AUR git clone — `primitives/aur.py:216` `aur_clone`.
2. Source-sync git fetch — `primitives/git_ops.py:63` `git_fetch_and_compare`, reached via
   the `source_sync` scheduler.
3. VCS upstream peek — `primitives/vcs_pkgver.py:248` `git ls-remote`.
4. VCS pkgver resolve — `primitives/vcs_pkgver.py` `evaluate_vcs_pkgver`. Both an egress
   (`makepkg -od` fetches sources) and an execution (it runs `pkgver()` and every top-level
   PKGBUILD statement). See [The `--devel` execution path](#devel-exec).

**Not gated:** AUR RPC info/search/`packages.gz` (`aur.py:102-105`). Metadata only.
Leaving it open keeps version reporting accurate under freeze — the user still *sees* that
an update exists and is simply refused the pull. Informed refusal beats blind refusal.

**Warned, not gated:** makepkg's own `source=()` downloads. SysForge does not mediate
makepkg's network, and refusing to build would defeat the "rebuild from disk" requirement.
The freeze reports what it cannot cover (see [The `source=()` warning](#the-source-warning)).

## Behaviour

### Failure mode

A denied download blocks **that package** and lets the run continue; the run exits
non-zero with the blocked set named in the summary. This matches how `STATUS_FAILED` /
`STATUS_RATE_LIMITED` / `STATUS_PURGE_REFUSED` already behave in the scheduler.

### Working under freeze

Existing flags already cover rebuilding on-disk sources and must not themselves trip the
gate, since they cause no egress:

- `sysforge build --no-update` (`cli.py:335`, `:375`) — skip the scheduler refresh, build
  the existing checkout.
- `sysforge update --offline` (`cli.py:403`) — `_sync_sources` returns early
  (`update_sync.py:98`); local version comparison only.

### Deliberate lifts

Run-scoped only. No persistent allowlist: stale trust entries are a silent hole, and
run-scoped lifts keep every lift visible in the unified run log.

## Architecture

A new leaf primitive, `sysforge/primitives/net_policy.py`:

```python
class NetworkFrozen(RuntimeError):
    """Raised when the source freeze denies an egress."""


@dataclass(frozen=True)
class NetPolicy:
    frozen: bool                 # gate active
    thawed: frozenset[str]       # pkgbases exempt this run

    def check(self, kind: str, pkgbase: str | None) -> None:
        """Raise NetworkFrozen if this egress is denied."""


def resolve_net_policy(args, cfg) -> NetPolicy
def set_policy(policy: NetPolicy) -> None
def get_policy() -> NetPolicy          # permissive default
def warn_ungated_sources(pkgbuild_dir: Path) -> list[str]
```

`net_policy.py` imports nothing from sysforge except `log`, keeping it in the leaf layer
per `tests/test_module_layering.py`.

The policy is resolved **once** at verb entry and stored module-globally; the three seams
consult `get_policy()`. The global is deliberate: the seams sit at very different depths
(`aur_clone` under the scheduler, `ls-remote` under `makepkg_wrapper`), and threading a
parameter through them reproduces the `--offline` weakness where a new call site defaults
to permissive. A global consulted at the seam **fails closed** for code that does not know
the gate exists. The cost is bounded by keeping the primitive small, frozen, and set once
— the same shape as the existing `source_sync.get_scheduler` singleton. `get_policy()`
returns a permissive `NetPolicy(frozen=False, thawed=frozenset())` when unset, so library
use and existing tests are unaffected.

### Enforcement points

| Seam | Location | On denial |
|---|---|---|
| AUR clone | `aur.py:216` `aur_clone` | raise `NetworkFrozen` |
| Source fetch | `git_ops.py:63` `git_fetch_and_compare` | scheduler catches → `STATUS_FROZEN` |
| VCS ls-remote | `vcs_pkgver.py:248` | return `None` + `warn()` |
| VCS pkgver resolve | `vcs_pkgver.py` `evaluate_vcs_pkgver` | return `None` → `DEVEL_EVAL_FAILED` |

The scheduler is the only caller that converts the exception into a status; `aur_clone`'s
other callers see the raise. Both `vcs_pkgver` seams degrade rather than raise because that
module's contract is already "`None` on any failure" with a defined fallback at every call
site — turning either into a hard error would break `--devel` in a way the freeze does not
intend.

<a id="devel-exec"></a>
### The `--devel` execution path

Gating the `ls-remote` peek **alone would make the freeze actively harmful**, and the
ordering at `update_version.py:250-263` is why:

1. `peek_upstream_commit` is the *cheap short-circuit* — if upstream HEAD still matches the
   SHA in `build_state.toml`, the package is `UP_TO_DATE` and nothing further runs.
2. `None` from that peek means "fall through to the canonical path".
3. The canonical path is `evaluate_vcs_pkgver` — which fetches sources and executes the
   PKGBUILD.

Denying only the peek therefore *raises* the probability of reaching the execution, for
every VCS package, on every frozen run. The two seams must be gated together.

`evaluate_vcs_pkgver` returns `None` under freeze, which `update_version.py:268` already
maps to the `DEVEL_EVAL_FAILED` action — an existing, non-fatal, per-package outcome. No new
status is required; the freeze reuses a decision path `--devel` already understands. The
`warn()` names the freeze as the cause so it is not mistaken for the transient-flake case
the action was built for.

**Cache-poisoning note.** `_RESOLVE_CMD` (`vcs_pkgver.py:42`) carries `--skippgpcheck`, so
the probe fetches into the shared `SRCDEST` with signature verification off. For entries
carrying real checksums a later build re-verifies and catches a swap; for `git+` / `SKIP`
entries — exactly the VCS packages this path serves — there is nothing to re-verify, so an
unverified clone placed by the *version check* silently becomes the *build's* input.
Gating this seam closes that hand-off under freeze. Removing `--skippgpcheck` outright is
**not** in scope: the probe would then fail on expired or unimported keys, which is the
transient-breakage case `DEVEL_EVAL_FAILED` exists to absorb, and the freeze is the right
control for a trust decision rather than the flag.

`source_sync.py` gains `STATUS_FROZEN = "frozen"` alongside the existing statuses
(`source_sync.py:92-101`), classified as a **blocker**. Reusing the blocker machinery
gives per-package refusal, summary rendering via `primitives/render.py`'s `[TAG]` gutter,
and the non-zero exit for free.

## Surface

### CLI

Global flags on the top-level parser, alongside `--color` / `--no-throttle` / `--turbo`
(`cli.py:1345-1375`) — one definition, applying to every verb.

| Flag | Effect |
|---|---|
| `--frozen` | Force the gate on for this run. |
| `--no-frozen` | Lift it for this run (overrides config). |
| `--thaw PKG[,PKG...]` | Exempt named pkgbases; the gate stays on for everything else. Repeatable. |

### Config

New `[security]` section in `etc/sysforge/sysforge.toml`:

```toml
[security]
# Refuse all source downloads (AUR clones, git fetches, VCS ls-remote).
# Existing checkouts still build. Lift per-run with --no-frozen or --thaw PKG.
freeze_sources = false
```

Ships `false` — the gate is opt-in, so a fresh install behaves as today. Live config
adopts it via `make sync-config`.

### Precedence

`--no-frozen` > `--frozen` > `[security] freeze_sources` > `false`.

The `--frozen`/config half goes through the existing one-home seam,
`config.resolve_flag_default(args, "frozen", cfg, "freeze_sources")`
(`primitives/config.py:204`). The explicit-off flag is a thin wrapper on top, since that
seam has no "explicit false" concept today.

`--no-frozen` exists deliberately: a gate with no documented off-switch gets bypassed by
editing config instead, which is worse because it is persistent. A loud per-run flag keeps
every lift in the run log.

<a id="the-source-warning"></a>
### The `source=()` warning

`net_policy.warn_ungated_sources(pkgbuild_dir)` runs on the build path when frozen. It
parses the PKGBUILD's `source` array, classifies each entry as local-file /
already-cached-in-`SRCDEST` / remote-and-uncached, and warns only on the last class — a
warning that fires on every build is a warning nobody reads.

```
[FREEZE] mesa: 2 sources makepkg will fetch outside the gate:
           https://archive.mesa3d.org/mesa-25.2.0.tar.xz
           git+https://gitlab.freedesktop.org/mesa/mesa.git
```

**Parsing detail (must not be missed):** `source` is *not* a member of
`_ARCH_ARRAY_FAMILIES` (`pkgbuild_meta.py:219`), so `source_<arch>` arrays are not merged
into the canonical key. PKGBUILD(5) permits `source_x86_64=()`. The warning therefore
reads `source` **and** every `source_<arch>` key explicitly. `sysforge/CLAUDE.md`'s "never
read `_<arch>` keys" rule applies to the merged families listed in
`_ARCH_ARRAY_FAMILIES`, not to `source`. Under-reporting here would be a silent gap in a
security warning.

Cached-file classification uses `pacman.get_srcdest()` per the makepkg-path-resolution
invariant — never `os.environ["SRCDEST"]`.

Log level is `warn()` per the rubric in `docs/design/12-logging.md`: narration about a
risk, not the answer the user asked for.

## Testing

- `frozen_policy` fixture in `tests/conftest.py` setting and tearing down the module
  policy.
- Per-seam denial tests: `aur_clone` raises `NetworkFrozen`; the scheduler reports
  `STATUS_FROZEN`; both `vcs_pkgver` seams return `None` and warn.
- **Fall-through regression test** (the reason this seam pair exists): a VCS package under
  `--devel` with a recorded `built_upstream_commit`, frozen, must reach `DEVEL_EVAL_FAILED`
  **without `makepkg` being invoked at all**. Assert on a patched `subprocess.run` — a test
  that only checks the returned action would pass while the execution still happened.
- `--thaw` test: one pkgbase syncs while its neighbour reports `STATUS_FROZEN` in the same
  run.
- Precedence tests covering all four rows, including `--no-frozen` overriding
  `freeze_sources = true`.
- Blocker-semantics test: a frozen package does not abort the run, and the run exits
  non-zero with the package named in the summary.
- `--no-update` and `--offline` under freeze: build succeeds, no denial raised.
- `warn_ungated_sources` tests: remote-uncached warns; local and cached entries do not;
  an arch-split `source_x86_64` PKGBUILD is fully reported.

## Lockstep obligations

Per project conventions, in the same change:

- Both completions — `completions/_sysforge` (zsh) and the bash completion — for the three
  new global flags.
- Manpage.
- `tools/check_shipped.py`: `_KNOWN_SECTIONS` += `security`.
- `etc/sysforge/sysforge.toml` and `tests/data/etc/sysforge/` fixture parity
  (`make check-shipped`).
- `docs/design/` update + `make design`; then README, then CLAUDE.md, in that order.
- Release-note entry for `3.0.0-F2` appended to `docs/release-notes/unreleased.md`,
  ID-first, in ascending ID order within its section.
- Remove `3.0.0-F2` from ROADMAP in the implementing commit.

## Out of scope

- Persistent per-package trust allowlists (rejected: stale entries are a silent hole).
- Gating AUR RPC metadata.
- Removing `--skippgpcheck` from `vcs_pkgver._RESOLVE_CMD` (see the cache-poisoning note) —
  the freeze gates the seam instead of weakening the probe's failure tolerance.
- Intercepting makepkg's `source=()` network (warned instead).
- Changing the review gate's non-TTY auto-accept — filed separately as `3.0.0-F3`, which
  closes the other half of the same threat model: this item stops hostile code *arriving*,
  `3.0.0-F3` stops already-arrived code building unreviewed. `3.0.0-F3` should reuse this
  item's blocker-reporting path rather than growing a second one.
