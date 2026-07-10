# Roadmap-ID Collision Detection Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `roadmap_ids` validator to `make check-standards` (plus a `--next-id` helper) that cross-checks ROADMAP.md against `docs/release-notes/` to catch collisions where an open roadmap ID reuses a shipped ID number.

**Architecture:** Two pure text→IDs extractors (ROADMAP bold-bullet anchor; release-notes HTML-comment-strip) feed a `check_roadmap_ids(repo)` function that emits `Finding`s for Planned↔shipped collisions, Planned↔Abandoned contradictions, and shipped `Q` IDs (errors, all prefixes), plus sequence gaps for the active `pyproject.toml` version prefix only (warn). A `--next-id <version>-<TYPE>` flag reuses the same extractors to print the next free number.

**Tech Stack:** Python 3.14 stdlib (`re`, `pathlib`, `argparse`), pytest via `make test`. All new code lives in `tools/check_standards.py`; tests in `tests/test_check_standards_roadmap_ids.py`.

## Global Constraints

- Every new `.py` line lives in existing files that already carry an `SPDX-License-Identifier: MIT` header — no new source file except the test module (which needs the standard 3-line SPDX header, copied verbatim from `tests/test_check_standards_changelog.py`).
- Findings use `Finding(group, severity, location, message)` positionally; `group="roadmap_ids"`, `severity` is `"error"` or `"warn"`.
- ID grammar is exactly `(\d+\.\d+\.\d+)-([A-Z]+)(\d+)`.
- Never invoke `pytest` directly — run tests via `make test` (or `make test-x`). For a single test, `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`.
- Do not commit unless the user explicitly asks — the "Commit" steps below stage + prepare a message only when instructed; otherwise stop after the passing test run.
- Active-cycle detection reads `version = "X.Y.Z"` from `pyproject.toml` at repo root.

---

### Task 1: ID extractors (two pure functions)

**Files:**
- Modify: `tools/check_standards.py` (add module-level constants + two functions + an `_parse_id` helper, after the existing `check_claude_md` block, before the `GROUPS` dict at line ~337)
- Test: `tests/test_check_standards_roadmap_ids.py` (create)

**Interfaces:**
- Produces:
  - `_parse_id(s: str) -> tuple[str, str, int] | None` — `"2.1.0-F1"` → `("2.1.0", "F", 1)`, non-matching → `None`.
  - `roadmap_ids_from_text(text: str) -> dict[str, str]` — maps each bold-bullet entry ID to `"planned"` or `"abandoned"`.
  - `shipped_ids_from_text(text: str) -> set[str]` — every ID token remaining after HTML comments are stripped.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_check_standards_roadmap_ids.py`:

```python
# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Unit guards for tools/check_standards.py::check_roadmap_ids and its extractors.

Guards the roadmap-ID collision detector: ROADMAP.md (open items) and
docs/release-notes/ (shipped items) are disjoint homes for the same ID space,
and nothing else cross-checks them, so an open ID can silently reuse a shipped
number (the 2.1.0-B2/B3 vs shipped B2-B7 incident this check exists to prevent).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "check_standards.py"


def _load():
    spec = importlib.util.spec_from_file_location("check_standards", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass Finding can resolve its
    # own __module__ in sys.modules (Python 3.14 dataclass annotation lookup).
    sys.modules["check_standards"] = mod
    spec.loader.exec_module(mod)
    return mod


check_standards = _load()


def test_parse_id_splits_version_type_number():
    assert check_standards._parse_id("2.1.0-F12") == ("2.1.0", "F", 12)
    assert check_standards._parse_id("not-an-id") is None


def test_roadmap_bold_bullet_is_extracted_as_planned():
    text = "## Planned\n\n- **`2.2.0-F7`** — a real entry.\n"
    assert check_standards.roadmap_ids_from_text(text) == {"2.2.0-F7": "planned"}


def test_roadmap_id_scheme_prose_is_not_extracted():
    """Inline examples in the `## ID scheme` section are prose, not entries."""
    text = "## ID scheme\n\nIDs are `<version>-<TYPE><n>`, e.g. `1.2.0-F1` (feature).\n"
    assert check_standards.roadmap_ids_from_text(text) == {}


def test_roadmap_bullet_after_abandoned_header_is_abandoned():
    text = (
        "## Planned\n\n- **`2.2.0-F1`** — live.\n\n"
        "## Abandoned / decided against\n\n- **`2.2.0-Q3`** — dropped.\n"
    )
    assert check_standards.roadmap_ids_from_text(text) == {
        "2.2.0-F1": "planned",
        "2.2.0-Q3": "abandoned",
    }


def test_roadmap_non_id_abandoned_entry_is_ignored():
    text = "## Abandoned\n\n- **`-sysforge` suffix** — scrapped.\n"
    assert check_standards.roadmap_ids_from_text(text) == {}


def test_shipped_ignores_html_comment_examples():
    """The accumulator's `<!-- e.g. (1.2.0-F35) -->` header must not count."""
    text = (
        "# sysforge (unreleased)\n\n"
        "<!-- Reference the roadmap ID inline, e.g. (1.2.0-F35). -->\n\n"
        "## Added\n\n- a real thing (2.2.0-F4).\n"
    )
    assert check_standards.shipped_ids_from_text(text) == {"2.2.0-F4"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: FAIL — `AttributeError: module 'check_standards' has no attribute '_parse_id'` (and the others).

- [ ] **Step 3: Write the extractors**

In `tools/check_standards.py`, immediately before the `GROUPS = {` line (~337), add:

```python
# ===========================================================================
# Group: roadmap_ids  (ROADMAP.md <-> release-notes ID collision detection)
# ===========================================================================

# ID grammar: <version>-<TYPE><n>, e.g. 2.1.0-F1 -> ("2.1.0", "F", 1).
_ID_RE = re.compile(r"(\d+\.\d+\.\d+)-([A-Z]+)(\d+)")
# A ROADMAP entry: a bullet whose first token is a bold code-span holding an ID.
# Anchoring here skips the `## ID scheme` section's inline prose examples.
_ROADMAP_ENTRY_RE = re.compile(r"^- \*\*`(\d+\.\d+\.\d+-[A-Z]+\d+)`")
# HTML comment blocks carry instructional prose (e.g. the accumulator header's
# "e.g. (1.2.0-F35)") whose IDs are examples, not entries — strip before scan.
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _parse_id(s: str) -> tuple[str, str, int] | None:
    m = _ID_RE.fullmatch(s)
    if not m:
        return None
    return m.group(1), m.group(2), int(m.group(3))


def roadmap_ids_from_text(text: str) -> dict[str, str]:
    """Map each bold-bullet ROADMAP entry ID to 'planned' or 'abandoned'."""
    state = "planned"
    out: dict[str, str] = {}
    for line in text.splitlines():
        if re.match(r"^##\s+Abandoned", line):
            state = "abandoned"
            continue
        m = _ROADMAP_ENTRY_RE.match(line)
        if m:
            out[m.group(1)] = state
    return out


def shipped_ids_from_text(text: str) -> set[str]:
    """Every ID token in release-notes text, after HTML comments are stripped."""
    stripped = _HTML_COMMENT_RE.sub("", text)
    return {m.group(0) for m in _ID_RE.finditer(stripped)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit** (only if the user asked to commit)

```bash
git add tools/check_standards.py tests/test_check_standards_roadmap_ids.py
git commit -m "feat(standards): roadmap-ID extractors for collision check (2.1.0-F1)"
```

---

### Task 2: `check_roadmap_ids` validation + group registration

**Files:**
- Modify: `tools/check_standards.py` (add `_project_version`, `_gather_ids`, `check_roadmap_ids`; add `"roadmap_ids": check_roadmap_ids` to `GROUPS`; update module docstring)
- Test: `tests/test_check_standards_roadmap_ids.py` (append validation tests)

**Interfaces:**
- Consumes: `roadmap_ids_from_text`, `shipped_ids_from_text`, `_parse_id`, `Finding`, `REPO` (Task 1 + existing).
- Produces:
  - `_project_version(repo: Path) -> str` — reads `version = "X.Y.Z"` from `pyproject.toml`.
  - `_gather_ids(repo: Path) -> tuple[dict[str,str], set[str]]` — `(roadmap_map, shipped_set)`.
  - `check_roadmap_ids(repo: Path) -> list[Finding]`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_check_standards_roadmap_ids.py`:

```python
def _mkrepo(tmp_path: Path, roadmap: str, notes: dict[str, str], version="2.2.0"):
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "sysforge"\nversion = "{version}"\n', encoding="utf-8"
    )
    (tmp_path / "ROADMAP.md").write_text(roadmap, encoding="utf-8")
    nd = tmp_path / "docs" / "release-notes"
    nd.mkdir(parents=True)
    for name, body in notes.items():
        (nd / name).write_text(body, encoding="utf-8")
    return tmp_path


def test_planned_id_also_shipped_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.1.0-B2`** — open bug.\n",
        {"v2.1.0.md": "# v2.1.0\n\n## Fixed\n\n- old bug (2.1.0-B2).\n"},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.1.0-B2" in f.message
               and "shipped" in f.message for f in findings), findings


def test_planned_id_also_abandoned_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — live.\n\n"
        "## Abandoned\n\n- **`2.2.0-F1`** — dropped.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.2.0-F1" in f.message
               and "Abandoned" in f.message for f in findings), findings


def test_shipped_q_id_is_error(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n_(none)_\n",
        {"v2.2.0.md": "# v2.2.0\n\n## Changed\n\n- shipped a question (2.2.0-Q4).\n"},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert any(f.severity == "error" and "2.2.0-Q4" in f.message for f in findings), findings


def test_active_prefix_gap_is_warn(tmp_path):
    # Active version 2.2.0; F1 and F3 present, F2 missing -> one warn.
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — a.\n- **`2.2.0-F3`** — c.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert [f for f in findings if f.severity == "warn" and "2.2.0-F2" in f.message]
    assert not [f for f in findings if f.severity == "error"]


def test_historical_prefix_gap_is_not_reported(tmp_path):
    # Active version 2.2.0; a 1.2.0 gap must stay silent (grandfathered).
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`1.2.0-F1`** — a.\n- **`1.2.0-F3`** — c.\n",
        {},
    )
    findings = check_standards.check_roadmap_ids(repo)
    assert not [f for f in findings if "1.2.0-F2" in f.message], findings


def test_real_repo_has_no_roadmap_id_errors():
    """The live tree must be collision-free (warns permitted)."""
    findings = check_standards.check_roadmap_ids(check_standards.REPO)
    errors = [f for f in findings if f.severity == "error"]
    assert errors == [], errors
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: FAIL — `check_roadmap_ids` not defined.

- [ ] **Step 3: Write the validation**

In `tools/check_standards.py`, after `shipped_ids_from_text` (from Task 1) and before the `GROUPS` dict, add:

```python
def _project_version(repo: Path) -> str:
    pyproject = repo / "pyproject.toml"
    if not pyproject.is_file():
        return ""
    for line in pyproject.read_text(encoding="utf-8").splitlines():
        m = re.match(r'\s*version\s*=\s*"(\d+\.\d+\.\d+)"', line)
        if m:
            return m.group(1)
    return ""


def _gather_ids(repo: Path) -> tuple[dict[str, str], set[str]]:
    roadmap: dict[str, str] = {}
    roadmap_md = repo / "ROADMAP.md"
    if roadmap_md.is_file():
        roadmap = roadmap_ids_from_text(roadmap_md.read_text(encoding="utf-8"))
    shipped: set[str] = set()
    notes_dir = repo / "docs" / "release-notes"
    if notes_dir.is_dir():
        files = sorted(notes_dir.glob("v*.md"))
        unreleased = notes_dir / "unreleased.md"
        if unreleased.is_file():
            files.append(unreleased)
        for md in files:
            shipped |= shipped_ids_from_text(md.read_text(encoding="utf-8"))
    return roadmap, shipped


def check_roadmap_ids(repo: Path) -> list[Finding]:
    findings: list[Finding] = []
    roadmap, shipped = _gather_ids(repo)
    planned = {i for i, s in roadmap.items() if s == "planned"}
    abandoned = {i for i, s in roadmap.items() if s == "abandoned"}

    # Check 1: an open (Planned) ID that reuses a shipped number.
    for i in sorted(planned & shipped):
        findings.append(Finding(
            "roadmap_ids", "error", "ROADMAP.md",
            f"{i} is open in ROADMAP but already shipped in release-notes "
            f"(collision — allocate a fresh ID via --next-id)",
        ))
    # Check 2: an ID that is both Planned and Abandoned.
    for i in sorted(planned & abandoned):
        findings.append(Finding(
            "roadmap_ids", "error", "ROADMAP.md",
            f"{i} is listed in both Planned and Abandoned sections",
        ))
    # Check 4: a Q-typed ID that shipped without promotion to F/B/STD.
    for i in sorted(shipped):
        parsed = _parse_id(i)
        if parsed and parsed[1] == "Q":
            findings.append(Finding(
                "roadmap_ids", "error", "docs/release-notes/",
                f"{i} is a Q-typed (open-question) ID that appears shipped — "
                f"a Q must be promoted to F/B/STD before implementation",
            ))
    # Check 5: sequence gaps, active version prefix only (warn).
    active = _project_version(repo)
    if active:
        by_type: dict[str, set[int]] = {}
        for i in planned | abandoned | shipped:
            p = _parse_id(i)
            if p and p[0] == active:
                by_type.setdefault(p[1], set()).add(p[2])
        for typ, nums in sorted(by_type.items()):
            for k in range(1, max(nums) + 1):
                if k not in nums:
                    findings.append(Finding(
                        "roadmap_ids", "warn", "ROADMAP.md",
                        f"{active}-{typ}{k} is missing from the {active}-{typ} "
                        f"sequence (gap in the active cycle)",
                    ))
    return findings
```

Then register the group — change the `GROUPS` dict:

```python
GROUPS = {
    "paths":       check_paths,
    "spdx":        check_spdx,
    "changelog":   check_changelog,
    "encoding":    check_encoding,
    "claude_md":   check_claude_md,
    "roadmap_ids": check_roadmap_ids,
}
```

And extend the module docstring's Groups list (after the `claude_md` entry, ~line 46) and its "Drift detection cases" list (~line 53):

```
    roadmap_ids Cross-checks ROADMAP.md (open items) against docs/release-notes/
                (shipped items): flags an open ID reusing a shipped number, an ID
                listed both Planned and Abandoned, a shipped Q-typed ID, and
                (warn) sequence gaps in the active pyproject version's prefix.
```

Add to the drift-case list:

```
    - Add an ID to ROADMAP Planned that already appears in a shipped v*.md.
    - List the same ID in both the Planned and Abandoned ROADMAP sections.
    - Reference a Q-typed ID in a shipped release-notes file.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: PASS (12 tests). If `test_real_repo_has_no_roadmap_id_errors` fails, a genuine live collision exists — stop and report it (do not weaken the test).

- [ ] **Step 5: Sanity-run the real check**

Run: `python tools/check_standards.py --check=roadmap_ids`
Expected: `[OK] roadmap_ids` or `[WARN] roadmap_ids` (warns allowed), summary line shows `0 error(s)`.

- [ ] **Step 6: Commit** (only if the user asked to commit)

```bash
git add tools/check_standards.py tests/test_check_standards_roadmap_ids.py
git commit -m "feat(standards): roadmap_ids collision check group (2.1.0-F1)"
```

---

### Task 3: `--next-id` allocation helper

**Files:**
- Modify: `tools/check_standards.py` (add `next_id`; add `--next-id` arg + early-return branch in `main`)
- Test: `tests/test_check_standards_roadmap_ids.py` (append)

**Interfaces:**
- Consumes: `_gather_ids`, `_parse_id` (Task 2).
- Produces: `next_id(repo: Path, prefix: str) -> str` — `prefix` like `"2.2.0-F"`, returns e.g. `"2.2.0-F8"`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_check_standards_roadmap_ids.py`:

```python
def test_next_id_returns_max_plus_one_including_abandoned(tmp_path):
    repo = _mkrepo(
        tmp_path,
        "## Planned\n\n- **`2.2.0-F1`** — a.\n\n"
        "## Abandoned\n\n- **`2.2.0-F5`** — dropped (still consumes the number).\n",
        {"v2.2.0.md": "# v2.2.0\n\n## Added\n\n- shipped (2.2.0-F3).\n"},
    )
    assert check_standards.next_id(repo, "2.2.0-F") == "2.2.0-F6"


def test_next_id_for_unused_type_starts_at_one(tmp_path):
    repo = _mkrepo(tmp_path, "## Planned\n\n_(none)_\n", {})
    assert check_standards.next_id(repo, "2.2.0-STD") == "2.2.0-STD1"


def test_next_id_flag_prints_and_exits_zero(tmp_path, capsys):
    _mkrepo(tmp_path, "## Planned\n\n- **`2.2.0-F2`** — a.\n", {})
    rc = check_standards.main(["--next-id", "2.2.0-F", "--repo", str(tmp_path)])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "2.2.0-F3"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: FAIL — `next_id` not defined / `--next-id` unrecognized.

- [ ] **Step 3: Implement the helper + wire the flag**

In `tools/check_standards.py`, after `check_roadmap_ids`, add:

```python
def next_id(repo: Path, prefix: str) -> str:
    """Next free ID for a '<version>-<TYPE>' prefix, across all three sources."""
    version, _, typ = prefix.partition("-")
    roadmap, shipped = _gather_ids(repo)
    nums = [
        p[2] for i in (set(roadmap) | shipped)
        if (p := _parse_id(i)) and p[0] == version and p[1] == typ
    ]
    return f"{version}-{typ}{(max(nums) + 1) if nums else 1}"
```

In `main`, add the argument alongside the others (after the `--repo` line):

```python
    p.add_argument("--next-id", metavar="VERSION-TYPE",
                   help="print the next free roadmap ID for a prefix "
                        "(e.g. 2.2.0-F) and exit")
```

And add an early-return branch right after `args = p.parse_args(argv)` (before the `--list` branch):

```python
    if args.next_id:
        print(next_id(args.repo.resolve(), args.next_id))
        return 0
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `make test ARGS="tests/test_check_standards_roadmap_ids.py -v"`
Expected: PASS (15 tests).

- [ ] **Step 5: Sanity-run the helper**

Run: `python tools/check_standards.py --next-id 2.2.0-F`
Expected: a single line like `2.2.0-F1` (max+1 over the live tree).

- [ ] **Step 6: Commit** (only if the user asked to commit)

```bash
git add tools/check_standards.py tests/test_check_standards_roadmap_ids.py
git commit -m "feat(standards): --next-id roadmap allocation helper (2.1.0-F1)"
```

---

### Task 4: Docs, standards entry, and same-commit ROADMAP removal

**Files:**
- Modify: `docs/design/21-standards.md` (document the `roadmap_ids` group + `--next-id`)
- Modify: `ROADMAP.md` (remove the `2.1.0-F1` entry — it is now implemented)
- Modify: `docs/release-notes/unreleased.md` (append the `2.1.0-F1` release-note entry, ascending ID order, under `## Added`)
- Run: `make design`, `make check-standards`, `make test`

**Interfaces:** none (docs + process).

- [ ] **Step 1: Document the group in the standards source**

Open `docs/design/21-standards.md`, find the section that enumerates `check_standards.py` groups (search for `claude_md` or `check_standards`), and add a `roadmap_ids` row/paragraph matching the existing style, e.g.:

```markdown
- **roadmap_ids** — cross-checks `ROADMAP.md` (open items) against
  `docs/release-notes/` (shipped items). Errors: an open ID reusing a shipped
  number, an ID in both Planned and Abandoned, a shipped `Q`-typed ID. Warn:
  sequence gaps within the active `pyproject.toml` version prefix. Allocate the
  next ID with `python tools/check_standards.py --next-id <version>-<TYPE>`.
```

- [ ] **Step 2: Regenerate DESIGN.md if the standards doc feeds it**

Run: `make design`
Expected: `DESIGN.md` regenerated (or no change). Then `make check-design` passes.

- [ ] **Step 3: Remove the implemented ROADMAP entry**

In `ROADMAP.md`, delete the entire `2.1.0-F1` bullet (lines beginning `- **\`2.1.0-F1\` — Collision-proof roadmap ID allocation…` through its `*Priority: medium…*` close). Per the process rule, implementing an item removes it from ROADMAP in the same commit — no "done" marker.

- [ ] **Step 4: Append the release-note entry**

In `docs/release-notes/unreleased.md`, under `## Added`, insert (keeping entries in ascending ID order — `2.1.0-F1` sorts among the `2.1.0-F*` block):

```markdown
- `check-standards`: new `roadmap_ids` group cross-checks ROADMAP.md against
  `docs/release-notes/` — flags an open ID reusing a shipped number, an ID in
  both Planned and Abandoned, and a shipped `Q`-typed ID; warns on sequence gaps
  in the active version prefix. Adds `--next-id <version>-<TYPE>` to allocate the
  next free ID monotonically (2.1.0-F1).
```

- [ ] **Step 5: Full guard + test sweep**

Run: `make check-standards`
Expected: `[OK]`/`[WARN] roadmap_ids` and every other group `[OK]`, `0 error(s)`. Crucially, `2.1.0-F1` no longer trips check 1 (it is now shipped-only, not Planned).

Run: `make test`
Expected: full suite passes (prior count + 15 new).

Run: `make check-design`
Expected: pass (DESIGN.md in sync).

- [ ] **Step 6: Commit** (only if the user asked to commit)

```bash
git add docs/design/21-standards.md DESIGN.md ROADMAP.md docs/release-notes/unreleased.md
git commit -m "docs(standards): document roadmap_ids group; land 2.1.0-F1"
```

---

## Self-Review

**Spec coverage:**
- Housing (group + `--next-id`, no new tool file) → Tasks 2 & 3. ✓
- Extractor: ROADMAP bold-bullet anchor → Task 1 (`roadmap_ids_from_text`) + tests for prose/abandoned/non-ID exclusion. ✓
- Extractor: release-notes comment-strip → Task 1 (`shipped_ids_from_text`) + comment-example test. ✓
- Check 1 (Planned↔shipped, error, all prefixes) → Task 2. ✓
- Check 2 (Planned↔Abandoned, error) → Task 2. ✓
- Check 4 (shipped Q, error) → Task 2. ✓
- Check 5 (gap, warn, active prefix only) → Task 2 (+ grandfathered-history test). ✓
- Dropped check 3 → not implemented (correct). ✓
- `--next-id` includes abandoned in max → Task 3 test asserts F5-abandoned bumps max. ✓
- Tests: extractor units, one positive per firing check, clean-tree, next-id → Tasks 1–3. ✓
- Docstring drift cases + `docs/design/21-standards.md` → Task 2 (docstring) + Task 4 (standards doc). ✓
- Same-commit ROADMAP removal + release note + self-validation → Task 4. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code; no "similar to Task N". ✓

**Type consistency:** `_parse_id` returns `tuple[str,str,int]|None` (used identically in Tasks 2 & 3); `_gather_ids` returns `(dict[str,str], set[str])` and is consumed the same way in `check_roadmap_ids` and `next_id`; `Finding(group, severity, location, message)` positional throughout; `roadmap_ids_from_text`/`shipped_ids_from_text` names stable across all tasks. ✓
