# Basic Package Management Verbs (`search` / `uninstall`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two everyday package-lifecycle verbs — `sysforge search <term>` (read-only, local→repo→AUR) and `sysforge uninstall <pkg>...` (mutating, `-Rnsu` + build-state demotion) — routed through the Verb framework.

**Architecture:** `uninstall` reuses the rename reverse-lookup already inline in `revert_cmd.plan_revert` by extracting it into a shared `install_reconcile.resolve_installed_name(bs, name)` helper, then chains `pacman.uninstall_pkgs` (`-Rnsu`) → `cmd_state_forget` → `BuildState.reconcile_external_installs` — the exact demotion composition `revert-to-stock` uses, no parallel path. `search` prints three fixed-order sections (local `-Qs`, repo `-Ss` via captured `--color always` passthrough, AUR via a new `aur.aur_search` RPC v5 helper), omitting any empty section; AUR failure is non-fatal.

**Tech Stack:** Python 3, argparse Verb framework (`sysforge/verbs/base.py`), pacman/pyalpm, AUR RPC v5 (urllib), pytest via `make test`.

## Global Constraints

- **Roadmap ID `1.2.0-F42`** — keeps its 1.2.0 prefix though `pyproject.toml` is at 2.2.0 (records origin cycle).
- **CLI verbs wire via `set_defaults(verb_cls=…)`**, never `func=`. Mutating verb sets `requires_sentinel = True`; read-only verb sets `requires_sentinel = False`.
- **One-home discipline:** rename resolution and build-state demotion have exactly one implementation each. Reuse `resolve_installed_name`, `cmd_state_forget`, and `reconcile_external_installs`; never reimplement.
- **`remove_pkgs` (existing) must not change** — `revert_cmd`'s `derename` path depends on its `-R --noconfirm` semantics.
- **Package management uses `uv`; tests run via Makefile targets** (`make test`, `make test-x`), never bare `pytest`. (Plan shows `uv run pytest <path>` only for single-test focus during a task; prefer `make test ARGS=...` where possible.)
- **Lockstep artifacts in the landing change:** `completions/_sysforge` + bash, manpage (scdoc), `docs/design/*.md` (+ `make design`) → README.md → CLAUDE.md (in that order), a `docs/release-notes/unreleased.md` entry tagged `1.2.0-F42` (Keep a Changelog, ascending ID order), and removal of `1.2.0-F42` from `ROADMAP.md`. Guards `make check-shipped` / `make check-standards` / `make check-design` stay green.
- **No commit/push unless the user explicitly asks.** The `git commit` steps below are written per skill convention but are DEFERRED — do the edits and run tests, then stop for the user to review. (User's global CLAUDE.md overrides the skill's commit steps.)
- **No dual-toolchain parity test** — neither verb branches on the resolved compiler (gcc vs llvm).

---

### Task 1: Extract `resolve_installed_name` helper (shared rename reverse-lookup)

Pull the stock-name → installed-`-sysforge`-name resolution out of `revert_cmd.plan_revert` into `install_reconcile`, and refactor `plan_revert` to call it. Behavior-preserving; the existing `test_revert_cmd.py` suite must stay green.

**Files:**
- Modify: `sysforge/primitives/install_reconcile.py` (add function)
- Modify: `sysforge/revert_cmd.py:60-105` (call the helper)
- Test: `tests/test_install_reconcile_resolve.py` (create)

**Interfaces:**
- Produces: `install_reconcile.resolve_installed_name(bs: BuildState, name: str) -> str` — returns the actually-installed pkgname for a user-supplied `name`. If `name` is a tracked entry key, returns it unchanged; else if some tracked entry has `origin_pkgbase == name`, returns that entry's key (lowest key wins, deterministic); else returns `name` unchanged (untracked / repo package).
- Consumes: `BuildState.all_packages() -> dict[str, dict]` (existing).

- [ ] **Step 1: Write the failing test**

Create `tests/test_install_reconcile_resolve.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for install_reconcile.resolve_installed_name (shared rename lookup)."""
from sysforge.primitives.build_state import BuildState
from sysforge.primitives import install_reconcile


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry  # direct seed for the test fixture
    return bs


def test_exact_key_returns_unchanged(tmp_path):
    bs = _bs(tmp_path, {"mesa": {"build_mode": "source_built", "pkgbase": "mesa"}})
    assert install_reconcile.resolve_installed_name(bs, "mesa") == "mesa"


def test_stock_base_resolves_to_renamed(tmp_path):
    bs = _bs(tmp_path, {
        "llvm-sysforge": {"build_mode": "pgo", "pkgbase": "llvm-sysforge",
                          "origin_pkgbase": "llvm"},
    })
    assert install_reconcile.resolve_installed_name(bs, "llvm") == "llvm-sysforge"


def test_untracked_returns_unchanged(tmp_path):
    bs = _bs(tmp_path, {})
    assert install_reconcile.resolve_installed_name(bs, "nano") == "nano"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_install_reconcile_resolve.py -q`
Expected: FAIL with `AttributeError: module 'sysforge.primitives.install_reconcile' has no attribute 'resolve_installed_name'`

- [ ] **Step 3: Add the helper**

Append to `sysforge/primitives/install_reconcile.py` (after the imports; add `from sysforge.primitives.build_state import BuildState` under `TYPE_CHECKING` is unnecessary — the param is untyped at runtime, so no new import is required):

```python
def resolve_installed_name(bs, name: str) -> str:
    """Resolve a user-supplied package name to its actually-installed name.

    A build that earned the ``-sysforge`` rename (conflict/coexist modes) is
    installed under the renamed name while recording the stock base in
    ``origin_pkgbase``. A user naming the stock base (``mesa``) should still
    reach the installed ``mesa-sysforge``. Resolution order:

      * exact tracked key            → returned unchanged
      * some entry's origin_pkgbase  → that entry's key (lowest key, so the
                                        result is deterministic across dict order)
      * otherwise (untracked / repo) → returned unchanged

    Single home for this reverse lookup — both ``revert_cmd`` and
    ``uninstall_cmd`` call it; never reimplement.
    """
    entries = bs.all_packages()
    if name in entries:
        return name
    for key in sorted(entries):
        if entries[key].get("origin_pkgbase") == name:
            return key
    return name
```

- [ ] **Step 4: Refactor `plan_revert` to use the helper**

In `sysforge/revert_cmd.py`, replace the reverse-lookup block (currently `revert_cmd.py:70-81`, the `if entry is None: … else: target_name = target`) with a call to the shared helper. The new body of the loop head becomes:

```python
    for target in targets:
        target_name = install_reconcile.resolve_installed_name(bs, target)
        entry = entries.get(target_name)
        if entry is None:
            plans.append(RevertPlan(target, "skip", None, None,
                                    "not tracked by sysforge — already stock"))
            continue
```

`install_reconcile` is already imported at `revert_cmd.py:41`. Leave the rest of the loop (the `mode` branching at lines 83-104) unchanged. Delete the now-dead `for name in sorted(entries): … else:` scan and the `else: target_name = target` arm.

- [ ] **Step 5: Run the new test + the revert suite**

Run: `uv run pytest tests/test_install_reconcile_resolve.py tests/test_revert_cmd.py -q`
Expected: PASS (all). The `test_plan_optimized_*` reverse-lookup cases confirm the refactor preserved behavior.

- [ ] **Step 6: Commit (DEFERRED — see Global Constraints)**

```bash
git add sysforge/primitives/install_reconcile.py sysforge/revert_cmd.py tests/test_install_reconcile_resolve.py
git commit -m "refactor(reconcile): extract resolve_installed_name shared rename lookup (1.2.0-F42)"
```

---

### Task 2: pacman primitives — `uninstall_pkgs`, `search_local`, `search_repo`

Add the pacman-side primitives the two verbs need. `uninstall_pkgs` is distinct from `remove_pkgs` (interactive `-Rnsu`, not `-R --noconfirm`), so `remove_pkgs` and `revert-to-stock` are untouched. Search helpers capture with forced colour so empty sections are detectable while native rendering is preserved.

**Files:**
- Modify: `sysforge/primitives/pacman.py` (add three functions near `remove_pkgs`, `pacman.py:581`)
- Test: `tests/test_pacman_pkg_verbs.py` (create)

**Interfaces:**
- Produces: `pacman.uninstall_pkgs(names: list[str], extra_flags: list[str] | None = None) -> None` — runs `sudo pacman -Rnsu [extra_flags…] -- <names>` with `check=True`, interactive (no `--noconfirm`, so pacman prints its own removal confirmation). No-op on empty list. Raises `subprocess.CalledProcessError` on non-zero exit.
- Produces: `pacman.search_local(term: str) -> str` — captured stdout of `pacman -Qs --color always <term>` (empty string when no match / exit≠0).
- Produces: `pacman.search_repo(term: str) -> str` — captured stdout of `pacman -Ss --color always <term>` (empty string when no match / exit≠0).

- [ ] **Step 1: Write the failing test**

Create `tests/test_pacman_pkg_verbs.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the pacman primitives backing `search` / `uninstall`."""
import subprocess
from types import SimpleNamespace

from sysforge.primitives import pacman


def test_uninstall_pkgs_builds_Rnsu_interactive(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append((a, k)) or SimpleNamespace(returncode=0))
    pacman.uninstall_pkgs(["mesa-sysforge"])
    (argv,), kwargs = calls[0]
    assert argv == ["sudo", "pacman", "-Rnsu", "--", "mesa-sysforge"]
    assert kwargs.get("check") is True
    assert "--noconfirm" not in argv  # interactive: pacman prompts


def test_uninstall_pkgs_forwards_extra_flags(monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: calls.append(a) or SimpleNamespace(returncode=0))
    pacman.uninstall_pkgs(["foo"], extra_flags=["-c"])
    assert calls[0][0] == ["sudo", "pacman", "-Rnsu", "-c", "--", "foo"]


def test_uninstall_pkgs_empty_is_noop(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran")))
    pacman.uninstall_pkgs([])  # must not call subprocess.run


def test_search_repo_returns_stdout_on_match(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=0, stdout="extra/nano 7.2-1\n"))
    assert pacman.search_repo("nano") == "extra/nano 7.2-1\n"


def test_search_local_empty_on_nonzero(monkeypatch):
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: SimpleNamespace(returncode=1, stdout=""))
    assert pacman.search_local("no-such-pkg") == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_pacman_pkg_verbs.py -q`
Expected: FAIL with `AttributeError: module 'sysforge.primitives.pacman' has no attribute 'uninstall_pkgs'`

- [ ] **Step 3: Add the primitives**

Insert into `sysforge/primitives/pacman.py` immediately after `reinstall_repo_pkgs` (after `pacman.py:606`):

```python
def uninstall_pkgs(names: list, extra_flags: list | None = None) -> None:
    """Remove packages via ``sudo pacman -Rnsu`` (interactive confirmation).

    Distinct from :func:`remove_pkgs` (``-R --noconfirm``, used by
    revert-to-stock before an immediate reinstall). Here the removal is the
    whole point, so:

      ``-n`` skip ``.pacsave`` backups; ``-s`` recurse now-orphaned deps;
      ``-u`` restrict recursion to packages nothing else needs (won't strand a
      still-required dep).

    No ``--noconfirm`` — pacman prints its own transaction + confirmation. No-op
    on an empty list. Raises ``subprocess.CalledProcessError`` on non-zero exit.
    """
    if not names:
        return
    argv = ["sudo", "pacman", "-Rnsu", *(extra_flags or []), "--", *names]
    subprocess.run(argv, check=True)


def _search(flag: str, term: str) -> str:
    """Run ``pacman <flag> --color always <term>``; return captured stdout.

    Forced colour preserves pacman's native rendering while capture lets the
    caller omit an empty section. Empty string on no match (exit != 0).
    """
    result = subprocess.run(
        ["pacman", flag, "--color", "always", term],
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else ""


def search_local(term: str) -> str:
    """Installed packages matching ``term`` (``pacman -Qs``). Empty if none."""
    return _search("-Qs", term)


def search_repo(term: str) -> str:
    """Sync-DB packages matching ``term`` (``pacman -Ss``). Empty if none."""
    return _search("-Ss", term)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_pacman_pkg_verbs.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit (DEFERRED)**

```bash
git add sysforge/primitives/pacman.py tests/test_pacman_pkg_verbs.py
git commit -m "feat(pacman): uninstall_pkgs + search_local/search_repo primitives (1.2.0-F42)"
```

---

### Task 3: `aur.aur_search` — RPC v5 name-desc search

Add the AUR search helper (no existing one). Mirrors `aur_info`'s request/error conventions; returns a list of result dicts, `[]` on any failure (non-fatal).

**Files:**
- Modify: `sysforge/primitives/aur.py` (add function after `aur_info`, `aur.py:134`; add `AUR_SEARCH_URL` near `AUR_RPC_URL`, `aur.py:99`; extend module docstring API list, `aur.py:12`)
- Test: `tests/test_aur_search.py` (create)

**Interfaces:**
- Produces: `aur.aur_search(term: str) -> list[dict]` — hits `https://aur.archlinux.org/rpc/v5/search/<term>?by=name-desc`, returns the `results` list (each a dict with at least `Name`, `Version`, `Description`). Returns `[]` on empty term, network error, timeout, or JSON error (logs a warning, never raises).

- [ ] **Step 1: Write the failing test**

Create `tests/test_aur_search.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for aur.aur_search (RPC v5 name-desc search)."""
import io
import json
import urllib.error

from sysforge.primitives import aur


class _Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_aur_search_parses_results(monkeypatch):
    payload = {"results": [
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ]}
    monkeypatch.setattr(aur.urllib.request, "urlopen",
                        lambda url, timeout=None: _Resp(json.dumps(payload).encode()))
    out = aur.aur_search("cosmic-ext-foo")
    assert out[0]["Name"] == "cosmic-ext-foo"


def test_aur_search_empty_term_returns_empty(monkeypatch):
    monkeypatch.setattr(aur.urllib.request, "urlopen",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("hit network")))
    assert aur.aur_search("") == []


def test_aur_search_network_error_is_nonfatal(monkeypatch):
    def _boom(url, timeout=None):
        raise urllib.error.URLError("down")
    monkeypatch.setattr(aur.urllib.request, "urlopen", _boom)
    assert aur.aur_search("anything") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_aur_search.py -q`
Expected: FAIL with `AttributeError: module 'sysforge.primitives.aur' has no attribute 'aur_search'`

- [ ] **Step 3: Add `AUR_SEARCH_URL` and `aur_search`**

In `sysforge/primitives/aur.py`, add next to `AUR_RPC_URL` (`aur.py:99`):

```python
AUR_SEARCH_URL    = "https://aur.archlinux.org/rpc/v5/search"
```

Add to the docstring API list (`aur.py:12`, under the `aur_info` line):

```
    aur_search(term)                  -> list[dict]        RPC v5 name-desc search (non-fatal)
```

Add the function after `aur_info` (`aur.py:134`):

```python
def aur_search(term: str) -> list[dict]:
    """Search the AUR by name+description via RPC v5 ``/search/<term>``.

    ``by=name-desc`` mirrors pacman ``-Ss`` (matches name and description), so
    the search verb's three sections behave consistently for one term. Returns
    the list of result dicts, or ``[]`` on empty term, network error, timeout,
    or malformed JSON — search never hard-fails on this optional third source.
    """
    if not term:
        return []
    url = f"{AUR_SEARCH_URL}/{urllib.parse.quote(term)}?by=name-desc"
    try:
        with urllib.request.urlopen(url, timeout=_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        _log.warn(f"AUR search failed: {e}")
        return []
    results = data.get("results", [])
    _log.info(f"AUR search '{term}': {len(results)} result(s)")
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_aur_search.py -q`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit (DEFERRED)**

```bash
git add sysforge/primitives/aur.py tests/test_aur_search.py
git commit -m "feat(aur): aur_search RPC v5 name-desc helper (1.2.0-F42)"
```

---

### Task 4: `uninstall` verb (`uninstall_cmd.py`, `UninstallVerb`)

The mutating verb. `pre_check` resolves names (pure) and builds a plan; `execute` prints the plan, removes via `pacman.uninstall_pkgs`, then demotes via `cmd_state_forget` + `reconcile_external_installs`.

**Files:**
- Create: `sysforge/uninstall_cmd.py`
- Test: `tests/test_uninstall_cmd.py` (create)

**Interfaces:**
- Consumes: `install_reconcile.resolve_installed_name` (Task 1), `pacman.uninstall_pkgs` (Task 2), `cmd_state_forget` (`sysforge/state_cmd.py:435`), `BuildState.reconcile_external_installs` + `install_reconcile.external_install_targets` (existing, see `revert_cmd.py:184-188`).
- Produces: `UninstallVerb` (`name = "uninstall"`, `requires_sentinel = True`) and `plan_uninstall(bs, targets) -> list[UninstallItem]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_uninstall_cmd.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the uninstall verb."""
from types import SimpleNamespace

from sysforge.primitives.build_state import BuildState
from sysforge import uninstall_cmd
from sysforge.verbs.base import PreCheckResult


def _bs(tmp_path, entries):
    bs = BuildState(tmp_path)
    for name, entry in entries.items():
        bs._data[name] = entry
    return bs


def test_requires_sentinel():
    assert uninstall_cmd.UninstallVerb.requires_sentinel is True


def test_plan_resolves_renamed_and_flags_tracked(tmp_path):
    bs = _bs(tmp_path, {
        "mesa-sysforge": {"build_mode": "fdo", "pkgbase": "mesa-sysforge",
                          "origin_pkgbase": "mesa"},
    })
    (item,) = uninstall_cmd.plan_uninstall(bs, ["mesa"])
    assert item.installed_name == "mesa-sysforge"
    assert item.tracked is True


def test_plan_untracked_passes_through(tmp_path):
    bs = _bs(tmp_path, {})
    (item,) = uninstall_cmd.plan_uninstall(bs, ["nano"])
    assert item.installed_name == "nano"
    assert item.tracked is False


def test_execute_removes_then_forgets_and_reconciles(tmp_path, monkeypatch):
    bs = _bs(tmp_path, {
        "mesa-sysforge": {"build_mode": "fdo", "pkgbase": "mesa-sysforge",
                          "origin_pkgbase": "mesa"},
    })
    order = []
    monkeypatch.setattr(uninstall_cmd.pacman, "uninstall_pkgs",
                        lambda names, extra_flags=None: order.append(("remove", names, extra_flags)))
    monkeypatch.setattr(uninstall_cmd, "cmd_state_forget",
                        lambda args: order.append(("forget", list(args.pkgnames))))
    monkeypatch.setattr(uninstall_cmd.install_reconcile, "external_install_targets",
                        lambda: set())

    verb = uninstall_cmd.UninstallVerb()
    args = SimpleNamespace(packages=["mesa"], pacman_flags=[], state_dir=str(tmp_path))
    pre = PreCheckResult(ctx={"items": uninstall_cmd.plan_uninstall(bs, ["mesa"]),
                              "state_dir": str(tmp_path)})
    res = verb.execute(args, pre)

    assert res.exit_code == 0
    assert order[0] == ("remove", ["mesa-sysforge"], [])
    assert order[1] == ("forget", ["mesa-sysforge"])  # forget runs after removal
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_uninstall_cmd.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sysforge.uninstall_cmd'`

- [ ] **Step 3: Write the verb**

Create `sysforge/uninstall_cmd.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""uninstall_cmd.py — the ``sysforge uninstall`` verb.

Remove packages and, for a sysforge-tracked package, demote it out of the
build-state authority so ``sysforge update`` stops rebuilding it. A naive
``pacman -R`` is wrong here on two counts: it leaves the ``build_state.toml``
record in place, and it doesn't know an optimized build may be installed under
a ``-sysforge`` renamed name. Resolution and demotion reuse the single homes
(``install_reconcile.resolve_installed_name`` + ``cmd_state_forget`` +
``reconcile_external_installs``) — no parallel path.
"""
from __future__ import annotations

import argparse
import subprocess
from dataclasses import dataclass

from sysforge import log
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives import install_reconcile, pacman
from sysforge.primitives.build_state import BuildState
from sysforge.state_cmd import cmd_state_forget
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("UNINSTALL")


@dataclass
class UninstallItem:
    """One target resolved to its installed name + tracking status."""

    target: str
    installed_name: str
    tracked: bool


def plan_uninstall(bs: BuildState, targets: list) -> list:
    """Resolve each target to its installed name + tracked flag. Pure."""
    entries = bs.all_packages()
    items: list[UninstallItem] = []
    for target in targets:
        installed = install_reconcile.resolve_installed_name(bs, target)
        items.append(UninstallItem(target, installed, installed in entries))
    return items


class UninstallVerb(Verb):
    """Remove package(s) and demote any sysforge-tracked ones."""

    name = "uninstall"
    requires_sentinel = True

    def pre_check(self, args) -> PreCheckResult:
        state_dir, _ = resolve_state_dir(getattr(args, "state_dir", None))
        bs = BuildState(state_dir)
        items = plan_uninstall(bs, list(args.packages))
        return PreCheckResult(ctx={"items": items, "state_dir": state_dir})

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        items = pre.ctx["items"]
        for it in items:
            renamed = "" if it.installed_name == it.target else f" (installed as {it.installed_name})"
            tag = "sysforge-tracked" if it.tracked else "repo/untracked"
            _log.ui(f"[uninstall] {it.target}{renamed} — {tag}")

        names = [it.installed_name for it in items]
        try:
            # Interactive: pacman prints its own transaction + confirmation.
            pacman.uninstall_pkgs(names, extra_flags=list(getattr(args, "pacman_flags", []) or []))
        except subprocess.CalledProcessError as exc:
            _log.error(f"[uninstall] pacman removal failed ({exc}); nothing demoted")
            return ExecResult(exit_code=1)

        # Demote tracked packages: forget their build_state records (handles
        # split-package siblings by pkgbase), then reconcile as belt-and-braces.
        tracked = [it.installed_name for it in items if it.tracked]
        if tracked:
            cmd_state_forget(argparse.Namespace(pkgnames=tracked, state_dir=pre.ctx["state_dir"]))
            bs = BuildState(pre.ctx["state_dir"])
            demoted = bs.reconcile_external_installs(install_reconcile.external_install_targets())
            if demoted:
                bs.save()
        return ExecResult(exit_code=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_uninstall_cmd.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit (DEFERRED)**

```bash
git add sysforge/uninstall_cmd.py tests/test_uninstall_cmd.py
git commit -m "feat(uninstall): uninstall verb with build-state demotion (1.2.0-F42)"
```

---

### Task 5: `search` verb (`search_cmd.py`, `SearchVerb`)

Read-only verb: three fixed-order sections, each with a header, each omitted when empty; AUR failure non-fatal.

**Files:**
- Create: `sysforge/search_cmd.py`
- Test: `tests/test_search_cmd.py` (create)

**Interfaces:**
- Consumes: `pacman.search_local` / `pacman.search_repo` (Task 2), `aur.aur_search` (Task 3).
- Produces: `SearchVerb` (`name = "search"`, `requires_sentinel = False`) and `render_aur(results) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_search_cmd.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the search verb."""
from types import SimpleNamespace

from sysforge import search_cmd
from sysforge.verbs.base import PreCheckResult


def test_not_sentinel_gated():
    assert search_cmd.SearchVerb.requires_sentinel is False


def test_render_aur_formats_repo_line():
    out = search_cmd.render_aur([
        {"Name": "cosmic-ext-foo", "Version": "1.0-1", "Description": "a thing"},
    ])
    assert "aur/cosmic-ext-foo 1.0-1" in out
    assert "a thing" in out


def test_sections_ordered_and_empty_omitted(monkeypatch, capsys):
    monkeypatch.setattr(search_cmd.pacman, "search_local", lambda t: "")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "extra/nano 7.2-1\n")
    monkeypatch.setattr(search_cmd.aur, "aur_search",
                        lambda t: [{"Name": "nano-git", "Version": "r1-1", "Description": "d"}])

    verb = search_cmd.SearchVerb()
    args = SimpleNamespace(term="nano")
    verb.execute(args, PreCheckResult(ctx={}))
    out = capsys.readouterr().out

    assert "Installed" not in out          # local empty → header omitted
    assert out.index("Repo") < out.index("AUR")  # fixed order
    assert "extra/nano" in out and "aur/nano-git" in out


def test_aur_failure_is_nonfatal(monkeypatch, capsys):
    monkeypatch.setattr(search_cmd.pacman, "search_local", lambda t: "local/foo 1-1\n")
    monkeypatch.setattr(search_cmd.pacman, "search_repo", lambda t: "")
    monkeypatch.setattr(search_cmd.aur, "aur_search", lambda t: [])  # helper already swallows errors

    verb = search_cmd.SearchVerb()
    res = verb.execute(SimpleNamespace(term="foo"), PreCheckResult(ctx={}))
    assert res.exit_code == 0
    assert "local/foo" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_search_cmd.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'sysforge.search_cmd'`

- [ ] **Step 3: Write the verb**

Create `sysforge/search_cmd.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""search_cmd.py — the ``sysforge search`` verb.

Search three sources in fixed order — local (installed), repo (sync DBs), AUR
— printing a header per non-empty section. Local/repo are pacman passthroughs
(captured with forced colour so an empty section can be omitted while native
rendering is preserved); AUR is sysforge-rendered (no pacman equivalent) and
its failure is non-fatal.
"""
from __future__ import annotations

from sysforge import log
from sysforge.primitives import aur, pacman
from sysforge.verbs.base import ExecResult, PreCheckResult, Verb

_log = log.get_logger("SEARCH")


def render_aur(results: list) -> str:
    """Render AUR results as pacman-style ``aur/name version`` + indented desc."""
    lines = []
    for r in results:
        name = r.get("Name", "?")
        ver = r.get("Version", "")
        desc = r.get("Description") or ""
        lines.append(f"aur/{name} {ver}")
        if desc:
            lines.append(f"    {desc}")
    return "\n".join(lines) + ("\n" if lines else "")


class SearchVerb(Verb):
    """Search installed, repo, and AUR packages for a term."""

    name = "search"
    requires_sentinel = False

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        term = args.term
        sections = [
            ("Installed", pacman.search_local(term)),
            ("Repo", pacman.search_repo(term)),
            ("AUR", render_aur(aur.aur_search(term))),
        ]
        for header, body in sections:
            if body.strip():
                _log.ui(f"== {header} ==")
                _log.ui(body.rstrip("\n"))
        return ExecResult(exit_code=0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_search_cmd.py -q`
Expected: PASS (4 passed). If `_log.ui` does not write to stdout under capsys, adjust the test to capture via the logger's stream (check how `tests/test_revert_cmd.py` or sibling verb tests assert on `_log.ui` output) — mirror that existing pattern rather than inventing one.

- [ ] **Step 5: Commit (DEFERRED)**

```bash
git add sysforge/search_cmd.py tests/test_search_cmd.py
git commit -m "feat(search): search verb across local/repo/AUR (1.2.0-F42)"
```

---

### Task 6: CLI wiring + completions + manpage (lockstep)

Register both verbs in argparse and keep the completion + manpage surfaces in lockstep (project convention: same change).

**Files:**
- Modify: `sysforge/cli.py` (imports near `cli.py:55`; two new `_add_*_parser` functions modeled on `_add_revert_parser` at `cli.py:713`; call them where the other `_add_*_parser(sub)` calls are registered)
- Modify: `completions/_sysforge` (zsh) and the bash completion file (find via `ls completions/`)
- Modify: the scdoc manpage source (find via `ls docs/ *.scd` / `git ls-files '*.scd'`)
- Test: `tests/test_cli_pkg_verbs.py` (create) + the completions parity agent

**Interfaces:**
- Consumes: `UninstallVerb`, `SearchVerb`.

- [ ] **Step 1: Write the failing CLI test**

Create `tests/test_cli_pkg_verbs.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""CLI wiring for search / uninstall verbs."""
from sysforge.cli import build_parser  # adjust import to the actual parser factory
from sysforge.search_cmd import SearchVerb
from sysforge.uninstall_cmd import UninstallVerb


def test_search_wires_verb_cls():
    ns = build_parser().parse_args(["search", "nano"])
    assert ns.verb_cls is SearchVerb
    assert ns.term == "nano"


def test_uninstall_wires_verb_cls():
    ns = build_parser().parse_args(["uninstall", "mesa"])
    assert ns.verb_cls is UninstallVerb
    assert ns.packages == ["mesa"]
```

Before running, confirm the parser factory name: `grep -n "def build_parser\|def make_parser\|def _build_parser\|ArgumentParser(" sysforge/cli.py` and fix the import + call accordingly.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_pkg_verbs.py -q`
Expected: FAIL (`SystemExit: 2`, "invalid choice: 'search'") — verbs not registered yet.

- [ ] **Step 3: Wire the verbs in `sysforge/cli.py`**

Add imports near `cli.py:55`:

```python
from sysforge.search_cmd import SearchVerb
from sysforge.uninstall_cmd import UninstallVerb
```

Add two parser builders next to `_add_revert_parser` (`cli.py:713`):

```python
def _add_search_parser(sub):
    p = sub.add_parser("search",
        help="search installed, repo, and AUR packages for a term")
    p.add_argument("term", metavar="TERM", help="search term (name + description)")
    p.set_defaults(verb_cls=SearchVerb)


def _add_uninstall_parser(sub):
    p = sub.add_parser("uninstall",
        help="remove package(s); demote any sysforge-tracked ones out of build state")
    p.add_argument("packages", nargs="+", metavar="PKG",
        help="package name(s) to remove (stock or -sysforge name)")
    p.add_argument("pacman_flags", nargs="*", metavar="-- PACMAN_FLAG",
        help="extra flags forwarded to pacman -Rnsu")
    p.set_defaults(verb_cls=UninstallVerb)
```

Note on `pacman_flags`: verify how argparse separates the positional `packages` (nargs="+") from `pacman_flags` (nargs="*") — two greedy positionals collide. If parity is a problem, drop `pacman_flags` from the CLI (the spec's forwarding is optional) and pass `extra_flags=[]`; keep the primitive's `extra_flags` param for internal callers. Decide by running the CLI test; prefer the simpler single-positional form if the two-positional form is ambiguous.

Register both alongside the sibling `_add_*_parser(sub)` calls (grep `_add_revert_parser(sub)` to find the registration site).

- [ ] **Step 4: Run CLI test to verify it passes**

Run: `uv run pytest tests/test_cli_pkg_verbs.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Update zsh + bash completions**

Read `completions/_sysforge` and the bash file. Add `search` and `uninstall` to the verb list with the same structure sibling verbs use (`revert-to-stock` is the closest model: a verb taking package-name arguments). `search` takes one TERM; `uninstall` takes one or more PKG.

- [ ] **Step 6: Update the manpage (scdoc)**

Add `search` and `uninstall` entries to the verbs section of the `.scd` source, matching the surrounding format (synopsis line + description). Model on the `revert-to-stock` entry.

- [ ] **Step 7: Run the parity guard + completions audit**

Run: `make check-shipped`
Expected: PASS. Then dispatch the `completions-cli-parity` agent (or run its check) to confirm `completions/_sysforge` matches the new argparse tree. Fix any reported drift.

- [ ] **Step 8: Commit (DEFERRED)**

```bash
git add sysforge/cli.py tests/test_cli_pkg_verbs.py completions/ docs/
git commit -m "feat(cli): wire search + uninstall verbs; completions + manpage (1.2.0-F42)"
```

---

### Task 7: Docs, release note, ROADMAP removal, full guard sweep

Land the documentation in the mandated order, record the release note, remove the roadmap item, and confirm every guard is green + the whole suite passes.

**Files:**
- Modify: the relevant `docs/design/*.md` fragment(s) — find the CLI/verbs section via `grep -rl "revert-to-stock" docs/design/`
- Run: `make design` (regenerates `DESIGN.md` — never edit `DESIGN.md` directly)
- Modify: `README.md` (user-facing verb list/usage)
- Modify: `docs/release-notes/unreleased.md` (add `1.2.0-F42` entry, ascending ID order)
- Modify: `ROADMAP.md` (remove the `1.2.0-F42` entry at `ROADMAP.md:97-104`)

- [ ] **Step 1: Update the design fragment**

In the `docs/design/*.md` fragment covering CLI verbs, add `search` and `uninstall` to the verb inventory, describing: `uninstall` = `pacman -Rnsu` + rename resolution (`resolve_installed_name`) + demotion (`cmd_state_forget` + reconcile); `search` = local→repo→AUR, AUR non-fatal. Keep DESIGN = implemented-only (no roadmap ID in the design text).

- [ ] **Step 2: Regenerate DESIGN.md**

Run: `make design`
Then: `make check-design`
Expected: both succeed; `git diff DESIGN.md` shows the new verbs.

- [ ] **Step 3: Update README.md**

Add `search` and `uninstall` to the user-facing verb list with a one-line usage example each. Keep it user-instructions-only (no dev workflow — that's not README scope).

- [ ] **Step 4: Add the release note**

Append to `docs/release-notes/unreleased.md` under the appropriate Keep-a-Changelog section (e.g. `### Added`), in ascending roadmap-ID order:

```markdown
- `sysforge search <term>` and `sysforge uninstall <pkg>...` — everyday
  package-lifecycle verbs. `search` spans installed → repo → AUR; `uninstall`
  runs `pacman -Rnsu` and, for a sysforge-tracked package, demotes it out of
  build state so `update` stops rebuilding it (reuses the revert-to-stock
  rename resolution + state-forget path). (`1.2.0-F42`)
```

- [ ] **Step 5: Remove the roadmap item**

Delete the entire `1.2.0-F42` entry from `ROADMAP.md` (`ROADMAP.md:97-104`) — git history is the record; no "done" marker. Confirm the Features list stays in ascending ID order after removal.

- [ ] **Step 6: Full guard + test sweep**

Run: `make check-shipped && make check-standards && make check-design && make lint && make test`
Expected: all green; the full suite passes (baseline was 3608 tests — expect it higher with the new tests).

- [ ] **Step 7: Commit (DEFERRED — the landing commit)**

```bash
git add DESIGN.md docs/ README.md ROADMAP.md
git commit -m "docs(1.2.0-F42): search/uninstall verbs — design, README, release note; drop from ROADMAP"
```

Then delete the spec + this plan per the completed-spec cleanup convention (`docs/superpowers/specs/2026-07-10-basic-package-verbs-design.md` and `docs/superpowers/plans/2026-07-10-basic-package-verbs.md`) once the work merges.

---

## Self-Review

**Spec coverage:**
- CLI surface (both verbs, `set_defaults(verb_cls=…)`, sentinel flags) → Task 6 (+ verb classes in Tasks 4/5). ✓
- `uninstall` resolve via extracted `resolve_installed_name` → Task 1. ✓
- `uninstall` `-Rnsu` removal → Task 2 (`uninstall_pkgs`) + Task 4. ✓
- `uninstall` forget + reconcile (no parallel path) → Task 4. ✓
- `search` local/repo passthrough + AUR `aur_search` + fixed order + empty-omit + AUR non-fatal → Tasks 2, 3, 5. ✓
- Testing plan (resolve across modes, verb call-order, `aur_search` parse + error, section ordering/omission, no dual-toolchain test) → Tasks 1,3,4,5. ✓
- Lockstep artifacts (completions, manpage, docs order, release note, ROADMAP removal, guards) → Tasks 6, 7. ✓

**Placeholder scan:** No TBD/TODO left as work items; the two "verify the exact name" notes (parser factory name in Task 6, `_log.ui` capture in Task 5) are explicit verification steps with a named fallback, not hand-waving. ✓

**Type consistency:** `resolve_installed_name(bs, name) -> str` defined Task 1, consumed identically in Tasks 4/5's plan functions. `uninstall_pkgs(names, extra_flags=None)` defined Task 2, called identically Task 4. `aur_search(term) -> list[dict]` defined Task 3, consumed Task 5 via `render_aur`. `UninstallItem.installed_name`/`.tracked` and `render_aur`/`plan_uninstall`/`plan_uninstall` names consistent across tasks. ✓
