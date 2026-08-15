# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Ordered plan for the kernel PKGBUILD's kconfig region (2.5.1-F1).

A kernel ``prepare()`` needs up to five things done to its kconfig steps, and
they only work in one order: seed a base ``.config``, overlay the sysforge
fragment, run the configured generation targets, re-enable hotplug drivers the
minimizer stripped, then let the operator review the result.

Those five used to be four independent patchers that each re-parsed the
previous one's output, coordinating through a ``# sysforge: kconfig-resolve``
comment so they could tell their own text apart from the packager's. Here the
order is data — :data:`SLOT_ORDER` — and contributors fill slots *by key*, so
the order they run in cannot affect the result. Nothing reads anything another
step wrote: :meth:`KconfigPlan.install` renders the whole region once.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from sysforge import log

_log = log.get_logger(__name__)

# --- slot vocabulary -------------------------------------------------------
BASE_SEED = "base_seed"
FRAGMENT_MERGE = "fragment_merge"
GENERATE = "generate"
HOTPLUG_MERGE = "hotplug_merge"
REVIEW = "review"
VERIFY = "verify"

#: The single ordering authority. Rendering walks this tuple; a step's position
#: here is the only thing that decides where its lines land.
SLOT_ORDER: tuple[str, ...] = (
    BASE_SEED, FRAGMENT_MERGE, GENERATE, HOTPLUG_MERGE, REVIEW, VERIFY,
)

# --- regions ---------------------------------------------------------------
# Two splice points, not one: when no generation targets are configured the
# packager's own kconfig lines survive, and the two groups attach to different
# lines. The seed/overlay follows the kconfig-*setup* line; the hotplug
# re-enable follows the *last* surviving kconfig line, so it still precedes a
# packager-owned review target. Both anchors insert after the line they match,
# so a packager-owned minimizer is followed by the seed/overlay, not preceded
# by it. When GENERATE is filled both regions resolve to the same offset and
# render as one contiguous block (the common case).
PRE = "pre"
POST = "post"

SLOT_REGION: dict[str, str] = {
    BASE_SEED: PRE,
    FRAGMENT_MERGE: PRE,
    GENERATE: POST,
    HOTPLUG_MERGE: POST,
    REVIEW: POST,
    VERIFY: POST,
}

HOTPLUG_FRAGMENT = "sysforge.hotplug.config"
DEFAULT_FRAGMENT = "sysforge.config"
BASE_CONFIG_FILE = "sysforge.base.config"

# The non-interactive kconfig-resolve target every rewrite/seed/merge step
# settles on. Used by every runtime consumer of the literal (the pass-3
# rewrite, its log line, and the three static-block step builders below) —
# the regex alternations that merely *recognize* the string (e.g.
# _KCONFIG_SETUP_RE, _ANY_KCONFIG_RE) are pattern fragments, not spelled-out
# values, and are left as literals.
OLDDEFCONFIG = "olddefconfig"

# --- text vocabulary -------------------------------------------------------
# Interactive kconfig targets — require terminal input or a TUI (bare `config`
# line-prompts for every symbol; the rest are full-screen UIs). Rewritten to
# `make olddefconfig` on unattended runs. `config` sits last in the alternation
# so `oldconfig` can't be half-matched (the preceding \s+ prevents mid-word
# matches either way).
# Groups: (1) leading whitespace + make + optional VAR=val args + space
#         (2) the interactive target name
#         (3) optional trailing whitespace / comment
_INTERACTIVE_KCONFIG_RE = re.compile(
    r"^(\s*make(?:\s+\w+=\S*)*\s+)(oldconfig|nconfig|menuconfig|xconfig|gconfig|config)"
    r"(\s*(?:#.*)?)$",
    re.MULTILINE,
)

# Review-only targets — the ncurses/X menus that constitute an operator review.
# `oldconfig` is deliberately EXCLUDED: it is a non-interactive *resolve* step
# (after the fragment merge it prompts for nothing), so it must not suppress
# sysforge's injected REVIEW slot (B17). _INTERACTIVE_KCONFIG_RE keeps
# `oldconfig` because its consumer rewrites it to `olddefconfig`.
_REVIEW_KCONFIG_RE = re.compile(
    r"^(\s*make(?:\s+\w+=\S*)*\s+)(nconfig|menuconfig|xconfig|gconfig|config)"
    r"(\s*(?:#.*)?)$",
    re.MULTILINE,
)

# Primary PRE anchor: a non-interactive kconfig-resolve line — the point where
# .config is established. Group 1 captures its indentation.
_KCONFIG_SETUP_RE = re.compile(
    r"^([ \t]*)make(?:\s+\w+=\S*)*\s+"
    r"(?:olddefconfig|oldconfig|defconfig|alldefconfig|localmodconfig|"
    r"localyesconfig|mod2yesconfig|allmodconfig)\b.*$",
    re.MULTILINE,
)

# Secondary PRE anchor: the line that creates .config, used when the PKGBUILD
# seeds .config without a make-config resolve step.
_CONFIG_WRITE_RE = re.compile(
    r"^([ \t]*)(?:cp\s+\S+\s+\.config|cat\b.*>\s*\.config)\b.*$",
    re.MULTILINE,
)

# Every target recognized as "a kconfig generation invocation" — the full set
# from `make help`'s config-targets section (kernel.toml ``kconfig_targets``
# validates against the same set; see resolve_kconfig_targets).
_ALL_KCONFIG_TARGETS = (
    "config|nconfig|menuconfig|xconfig|gconfig|oldconfig|olddefconfig|"
    "localmodconfig|localyesconfig|mod2yesconfig|defconfig|allmodconfig|"
    "alldefconfig|savedefconfig|listnewconfig|randconfig"
)
_ANY_KCONFIG_RE = re.compile(
    # Trailer is same-line only ([ \t], not \s): a multi-line \s* would let a
    # match swallow a following comment line and misclassify the invocation.
    r"^([ \t]*)(make(?:\s+\w+=\S*)*[ \t]+)(?:" + _ALL_KCONFIG_TARGETS
    + r")([ \t]*(?:#.*)?)$",
    re.MULTILINE,
)


def _find_kconfig_anchor(text: str) -> tuple[int, str] | None:
    """Where the PRE region goes: ``(line_start_offset, indent)`` or None.

    The offset is the start of the line *after* the anchor, so every splice in
    this module inserts at a line boundary with a block that ends in a newline.
    Prefers a non-interactive kconfig-resolve line; falls back to the ``.config``
    creation line.
    """
    m = _KCONFIG_SETUP_RE.search(text) or _CONFIG_WRITE_RE.search(text)
    if m is None:
        return None
    nl = text.find("\n", m.end())
    return (nl + 1 if nl != -1 else len(text)), m.group(1)


def _dedupe_trailer_token(lines: list[str]) -> list[str]:
    """Keep the ``{trailer}`` token on only the LAST line that carries it.

    Both :func:`generate_step` (its last rendered line) and
    :func:`ui_target_step` (its only line) carry the token, each written
    assuming it is the sole POST contributor that does. When a configured
    ``kconfig_targets`` sequence splits its UI tail into REVIEW, GENERATE and
    REVIEW are both filled and both land in POST — without this, the same
    captured anchor trailer would render twice (once on GENERATE's last line,
    again on REVIEW's), an unsanctioned divergence from the pre-refactor
    single-trailer placement (the trailer belongs on the last line overall).
    """
    idxs = [i for i, ln in enumerate(lines) if "{trailer}" in ln]
    if len(idxs) <= 1:
        return lines
    lines = list(lines)
    for i in idxs[:-1]:
        lines[i] = lines[i].replace("{trailer}", "")
    return lines


def _render(lines, *, indent: str, make: str = "make ", trailer: str = "") -> str:
    """Substitute the placeholder tokens and terminate every line.

    ``str.replace``, never ``str.format``: the lines carry shell quoting and
    ``$startdir``, and a literal brace would break formatting.
    """
    out = []
    for line in lines:
        out.append(
            line.replace("{indent}", indent)
                .replace("{make}", make)
                .replace("{trailer}", trailer)
        )
    return "\n".join(out) + "\n"


@dataclass(frozen=True)
class Step:
    """One slot's contribution.

    ``lines`` are stored indent-free with ``{indent}``/``{make}``/``{trailer}``
    tokens, substituted at install time by :func:`_render` (via ``str.replace``,
    not ``str.format`` — the lines contain shell quoting and ``$startdir``).

    ``skip_if_present`` is a substring that, when already in the PKGBUILD,
    means this step is redundant — the idempotency guard.

    ``noninteractive_rewrite``, when set, is what a non-interactive run
    substitutes for ``lines`` instead of dropping the step outright — used by
    :func:`ui_target_step` (a *configured* review target must still resolve
    the config non-interactively, matching the old
    ``patch_noninteractive_kconfig`` rewrite of a surviving UI target to
    :data:`OLDDEFCONFIG`). ``None`` means "drop on a non-interactive run", the
    behaviour every other step (including the injected :func:`review_step`)
    wants.

    ``owns_generation`` marks a step as *configured* kconfig generation —
    set by both :func:`generate_step` and :func:`ui_target_step` (the UI tail
    split off it is still part of the same configured sequence). Any step
    with this set means the configured sequence is the sole authority for
    kconfig generation, which drives two things in :meth:`KconfigPlan.install`:
    the removal pass fires (every packager-owned kconfig line goes, even when
    the configured sequence is UI-only and :data:`GENERATE` itself ends up
    empty), and the REVIEW slot survives the "packager already has a review
    target" drop rule (that rule exists to suppress a second *injected*
    review, not to delete the operator's own configured one).

    INVARIANT: no ``owns_generation=True`` step may also set
    ``skip_if_present`` — ``install()`` evaluates ``owns_generation`` on the
    *post-drop-rules* step dict (:meth:`KconfigPlan.install` pass 1 runs the
    ``skip_if_present`` idempotency drop before the removal-pass gate reads
    ``owns_generation``), so a marker already present in the PKGBUILD would
    silently drop the step *and* disable the removal pass with it —
    reintroducing the round-2 regression this field was added to fix.
    Neither current ``owns_generation`` builder (:func:`generate_step`,
    :func:`ui_target_step`) sets ``skip_if_present``; keep it that way, or
    teach the removal-pass gate to look at the pre-drop steps instead.
    ``tests/test_kconfig_plan.py``'s
    ``test_owns_generation_steps_never_set_skip_if_present`` asserts this
    structurally.
    """

    slot: str
    lines: tuple[str, ...]
    skip_if_present: str | None = None
    noninteractive_rewrite: tuple[str, ...] | None = None
    owns_generation: bool = False


class KconfigPlan:
    """Slots filled by key, rendered in :data:`SLOT_ORDER`."""

    def __init__(self) -> None:
        self._steps: dict[str, Step] = {}

    def contribute(self, step: Step) -> None:
        """Fill ``step.slot``. Refilling is a programming error, not a merge."""
        if step.slot not in SLOT_REGION:
            raise ValueError(f"unknown kconfig slot {step.slot!r}")
        if step.slot in self._steps:
            raise ValueError(f"kconfig slot {step.slot!r} is already filled")
        self._steps[step.slot] = step

    def drop(self, slot: str) -> None:
        """Remove ``slot`` if present. Idempotent."""
        self._steps.pop(slot, None)

    def has(self, slot: str) -> bool:
        return slot in self._steps

    def filled(self) -> tuple[str, ...]:
        """Filled slots in :data:`SLOT_ORDER`, never in contribution order."""
        return tuple(s for s in SLOT_ORDER if s in self._steps)

    def install(self, patched_path, *, noninteractive: bool = False) -> None:
        """Render every filled slot into the PKGBUILD's kconfig region, once.

        Four passes, in this order:

        1. **Drop rules.** A cooperating PKGBUILD (one that already calls
           ``merge_config.sh``) drops the PRE slots but keeps the rest — the
           packager owns the overlay, not the generation. A slot whose
           ``skip_if_present`` marker is already in the text drops itself
           (idempotency). An unattended run drops :data:`REVIEW` (or rewrites
           it to :data:`OLDDEFCONFIG` when it carries a
           ``noninteractive_rewrite`` — a configured UI tail still has to
           resolve the config). A PKGBUILD with a review target of its own
           also drops an *injected* REVIEW — but not a configured one
           (``owns_generation``): pass 2 is about to remove that packager
           menu line as part of the configured sequence's removal, so a
           configured REVIEW must survive to replace it, not vanish with it.
        2. **Removal**, whenever any filled step has ``owns_generation`` set
           (:func:`generate_step` or :func:`ui_target_step`, alone or
           together — a UI-only configured sequence still owns generation
           even though :data:`GENERATE` itself ends up unfilled): the
           configured sequence is the sole authority for kconfig generation,
           so every packager-owned kconfig line goes. Raises when there are
           none — never build with a half-patched config step.
        3. **Rewrite** of surviving packager-owned UI targets on an unattended
           run. Provably a no-op after pass 2 (which removes every kconfig
           line), which is why pass 2 may record its anchor offset up front.
        4. **Splice** the two regions, higher offset first so the lower one's
           offset stays valid; on a tie POST goes first so PRE — inserted
           second, at that same index — ends up ahead of it.

        Modifies ``patched_path`` (``PKGBUILD.sysforge``) in place.
        """
        path = Path(patched_path)
        text = path.read_text(encoding="utf-8")
        steps = dict(self._steps)

        # --- 1. drop rules
        if "merge_config.sh" in text:
            dropped_base = steps.pop(BASE_SEED, None)
            dropped_fragment = steps.pop(FRAGMENT_MERGE, None)
            if dropped_base or dropped_fragment:
                _log.info(
                    "PKGBUILD already applies a kconfig fragment "
                    "(merge_config.sh present) — skipping the seed/merge steps",
                )
        for slot, step in list(steps.items()):
            if step.skip_if_present and step.skip_if_present in text:
                del steps[slot]
        if noninteractive and REVIEW in steps:
            review = steps[REVIEW]
            if review.noninteractive_rewrite is not None:
                steps[REVIEW] = Step(
                    slot=REVIEW,
                    lines=review.noninteractive_rewrite,
                    skip_if_present=review.skip_if_present,
                    owns_generation=review.owns_generation,
                )
                _log.info(
                    f"Non-interactive run — rewriting the configured kconfig "
                    f"review target to {OLDDEFCONFIG}",
                )
            else:
                del steps[REVIEW]
                _log.info("Non-interactive run — dropping the kconfig review step")
        # This drop rule exists to suppress a second *injected* nconfig when
        # the packager already opens a review menu of their own — it must not
        # delete a *configured* REVIEW (owns_generation): pass 2 below is
        # about to remove that packager-owned menu line as part of the
        # configured sequence's sole-authority removal, so the configured
        # tail is the only review left standing, not a duplicate.
        if (REVIEW in steps and not steps[REVIEW].owns_generation
                and _REVIEW_KCONFIG_RE.search(text)):
            del steps[REVIEW]
            _log.info(
                "PKGBUILD already opens a kconfig review menu — "
                "not injecting a second one",
            )
        if not steps:
            return

        # --- 2. removal pass
        # (offset, indent, make_prefix, trailer) recorded from the first
        # removed match, or None when no step owns generation — kept as one
        # optional tuple (rather than three separately-`None`-initialized
        # locals) so every read of indent/make_prefix/trailer below is
        # provably a `str`, never `str | None`.
        #
        # Reads `steps` AFTER pass 1's drop rules (including the
        # `skip_if_present` idempotency drop) — see the INVARIANT on
        # `Step.owns_generation`: no `owns_generation` step may also set
        # `skip_if_present`, or its marker being in the text would silently
        # disable this gate along with dropping the step.
        removal_anchor: tuple[int, str, str, str] | None = None
        if any(s.owns_generation for s in steps.values()):
            matches = list(_ANY_KCONFIG_RE.finditer(text))
            if not matches:
                raise RuntimeError(
                    f"kconfig plan: no kconfig make invocation found in {path} "
                    f"— cannot anchor the configured target sequence"
                )
            first = matches[0]
            removal_anchor = (
                first.start(), first.group(1), first.group(2), first.group(3),
            )
            # Last-to-first so earlier offsets (removal_anchor's included) stay
            # valid. Swallow the terminating newline so no blank line is left
            # behind.
            for m in reversed(matches):
                start, end = m.start(), m.end()
                if end < len(text) and text[end] == "\n":
                    end += 1
                elif start > 0 and text[start - 1] == "\n":
                    start -= 1
                text = text[:start] + text[end:]

        # --- 3. non-interactive rewrite of surviving packager UI targets
        if noninteractive:
            rewrites = 0

            def _to_olddefconfig(m):
                nonlocal rewrites
                rewrites += 1
                _log.info(
                    f"Replaced interactive kconfig target {m.group(2)!r} with "
                    f"{OLDDEFCONFIG}: {m.group(0).strip()!r}",
                )
                return m.group(1) + OLDDEFCONFIG + m.group(3)

            text = _INTERACTIVE_KCONFIG_RE.sub(_to_olddefconfig, text)
            if not rewrites:
                _log.info("No interactive kconfig targets found — nothing replaced")

        # --- 4. anchors
        if removal_anchor is not None:
            # One contiguous block: both regions land where the packager's
            # kconfig lines used to be, sharing their indent and make prefix.
            removed_at, indent, make_prefix, trailer = removal_anchor
            pre_at = post_at = removed_at
            post_indent = indent
        else:
            anchor = _find_kconfig_anchor(text)
            if anchor is None:
                _log.warn(
                    "No kconfig anchor (make olddefconfig / .config seed) found in "
                    "the kernel PKGBUILD prepare() — cannot inject the sysforge "
                    "kconfig steps, so the hardware/device fragment will be "
                    "ignored. Add a `make olddefconfig` step in prepare().",
                )
                return
            pre_at, indent = anchor
            make_prefix, trailer = "make ", ""
            # POST carries its own indent: it anchors at a different line than
            # PRE does, and today's hotplug injection takes the indentation of
            # the line it lands before, not the seed line's.
            post_at, post_indent = self._post_anchor(text, default=(pre_at, indent))

        pre_lines = [ln for s in SLOT_ORDER if s in steps and SLOT_REGION[s] == PRE
                     for ln in steps[s].lines]
        post_lines = _dedupe_trailer_token(
            [ln for s in SLOT_ORDER if s in steps and SLOT_REGION[s] == POST
             for ln in steps[s].lines],
        )
        pre = _render(pre_lines, indent=indent, make=make_prefix, trailer=trailer) \
            if any(SLOT_REGION[s] == PRE for s in steps) else ""
        post = _render(post_lines, indent=post_indent, make=make_prefix, trailer=trailer) \
            if any(SLOT_REGION[s] == POST for s in steps) else ""

        # Insert whichever region has the higher offset first, so the lower
        # one's offset stays valid against the un-shifted text. When the two
        # offsets are EQUAL (the common contiguous-block case, where the
        # removal pass sets pre_at == post_at), POST is inserted first so PRE
        # — inserted second, at that same index — lands ahead of it, matching
        # SLOT_ORDER (PRE slots precede POST slots).
        if pre_at > post_at:
            if pre:
                text = text[:pre_at] + pre + text[pre_at:]
            if post:
                text = text[:post_at] + post + text[post_at:]
        else:
            if post:
                text = text[:post_at] + post + text[post_at:]
            if pre:
                text = text[:pre_at] + pre + text[pre_at:]

        path.write_text(text, encoding="utf-8")
        _log.info(
            f"Rendered kernel kconfig plan into {path.name}: "
            f"{', '.join(s for s in SLOT_ORDER if s in steps)}",
        )

    @staticmethod
    def _post_anchor(text: str, *, default: tuple[int, str]) -> tuple[int, str]:
        """Where the POST region goes when no removal pass ran, and its indent.

        Before the last surviving review target when there is one (the hotplug
        re-enable must be in the config the operator reviews), else after the
        last kconfig line of any kind (so a minimizer can never run after the
        merge), else the PRE anchor.

        The indent is taken from the matched anchor line itself. This is a
        deliberate change from ``pkgbuild_patcher.py``'s
        ``patch_hotplug_fragment_merge``, which reads the indent of the line
        *after* the insertion point — for a stock PKGBUILD, that's the
        closing ``}``, i.e. column 0. Column-0 `if`/`fi` guard blocks are
        shell-legal but read as misformatted next to the surrounding
        ``prepare()`` body; anchoring on the matched line's own indent instead
        renders a properly-nested block. Purely cosmetic — bash doesn't care
        about indentation — so it changes no runtime behaviour.
        """
        review = list(_REVIEW_KCONFIG_RE.finditer(text))
        if review:
            last = review[-1]
            line_start = text.rfind("\n", 0, last.start()) + 1
            indent_m = re.match(r"[ \t]*", text[line_start:])
            return line_start, (indent_m.group(0) if indent_m else "")
        any_matches = list(_ANY_KCONFIG_RE.finditer(text))
        if any_matches:
            last = any_matches[-1]
            nl = text.find("\n", last.end())
            return (nl + 1 if nl != -1 else len(text)), last.group(1)
        return default


# --- step builders ---------------------------------------------------------

def base_seed_step() -> Step:
    """Copy the resolved base config over ``.config``, then re-resolve.

    File-guarded, so the default ``base_config = "pkgbuild"`` (which writes no
    file) is a runtime no-op.
    """
    return Step(
        slot=BASE_SEED,
        lines=(
            "{indent}# sysforge: seed the base config (when provided), then merge the fragment",
            f'{{indent}}if [ -f "$startdir/{BASE_CONFIG_FILE}" ]; then',
            f'{{indent}}  cp "$startdir/{BASE_CONFIG_FILE}" .config',
            f"{{indent}}  make {OLDDEFCONFIG}",
            "{indent}fi",
        ),
    )


def fragment_merge_step(fragment: str = DEFAULT_FRAGMENT) -> Step:
    """Overlay the hardware/device/manual kconfig fragment onto ``.config``."""
    return Step(
        slot=FRAGMENT_MERGE,
        lines=(
            f'{{indent}}if [ -f "$startdir/{fragment}" ]; then',
            f'{{indent}}  ./scripts/kconfig/merge_config.sh -m .config "$startdir/{fragment}"',
            f"{{indent}}  make {OLDDEFCONFIG}",
            "{indent}fi",
        ),
        skip_if_present=fragment,
    )


def generate_step(targets: list[str]) -> Step:
    """The configured ``kernel.toml kconfig_targets`` sequence.

    Already validated and ordered by ``resolve_kconfig_targets``; any UI target
    is last, and the caller splits it into :data:`REVIEW` before contributing.

    ``{trailer}`` is placed only on the LAST rendered line, matching old
    ``patch_kconfig_targets`` (which appended the captured trailer once, after
    the whole joined block: ``"\\n".join(...) + trailer``) — a multi-target
    sequence must not repeat the anchor line's trailing comment on every
    generated line.

    ``owns_generation=True``: the configured sequence is the sole authority
    for kconfig generation, triggering the removal pass in
    :meth:`KconfigPlan.install` even when (paired with a UI-only tail split
    into REVIEW) this step ends up being the only owner of that flag.
    """
    last = len(targets) - 1
    return Step(
        slot=GENERATE,
        lines=tuple(
            f"{{indent}}{{make}}{t}" + ("{trailer}" if i == last else "")
            for i, t in enumerate(targets)
        ),
        owns_generation=True,
    )


def hotplug_merge_step(fragment: str = HOTPLUG_FRAGMENT) -> Step:
    """Re-enable hotplug driver classes as modules after minimization (F2)."""
    return Step(
        slot=HOTPLUG_MERGE,
        lines=(
            "{indent}# sysforge: re-enable hotplug drivers as modules after minimization (F2)",
            f'{{indent}}if [ -f "$startdir/{fragment}" ]; then',
            f'{{indent}}  ./scripts/kconfig/merge_config.sh -m .config "$startdir/{fragment}"',
            f"{{indent}}  make {OLDDEFCONFIG}",
            "{indent}fi",
        ),
        skip_if_present=fragment,
    )


#: Substring of :func:`verify_step`'s rendered text that proves the check is
#: already installed — the idempotency marker, and the string tests match on.
VERIFY_MARKER = "_sf_kconfig_verify"


def verify_step(
    fragments: tuple[str, ...] = (DEFAULT_FRAGMENT, HOTPLUG_FRAGMENT),
) -> Step:
    """Warn per symbol whose requested value did not survive into ``.config`` (F23).

    SysForge writes its fragments as plain text and, until this step, never
    checked that the symbols it asked for actually landed. Three mechanisms void
    a fragment line with no fragment-level signal: a value illegal for the
    symbol's type (``=m`` on a ``bool`` — kconfig discards the assignment and
    warns mid-build), a symbol upstream renamed or removed (dropped in silence),
    and an unmet *host-tooling* dependency (``CONFIG_RUST``, whose
    ``scripts/rust_is_available.sh`` probe fails and leaves the symbol unset).
    ``2.6.1-B17`` was the first two at once, and each survived because the only
    evidence was a warning scrolling past during a 20-minute build, erased from
    ``.config`` by the next ``make olddefconfig``.

    ``merge_config.sh`` already models the check (``Value requested for
    CONFIG_X not in final .config``); this does the equivalent for sysforge's
    own fragments. Three properties matter:

    * **Shell, not Python.** The resolved ``.config`` only exists inside
      ``prepare()``'s build tree, which sysforge never reads back.
    * **Warn, never fail.** A dropped symbol is a silent loss of intent, but
      hard-failing a kernel build over one stale entry in a curated table is
      worse. ``kernel_safety.py`` remains the only hard gate.
    * **Last in :data:`SLOT_ORDER`.** It reports on the ``.config`` the build
      actually uses, so it must run after :data:`REVIEW` — an operator editing
      the config in ``nconfig`` can drop a requested symbol too.

    Type-agnostic by construction: it compares the literal requested value
    against the literal resolved one, so it needs no table of symbol types and
    cannot drift as the kernel tree changes. An absent symbol and an ``is not
    set`` line both read as ``n``, which is what kconfig means by them.

    Errexit-safe under makepkg's ``set -e``: every failing command sits in an
    ``if`` condition or is swallowed by ``|| true`` at the call.
    """
    return Step(
        slot=VERIFY,
        lines=(
            "{indent}# sysforge: warn on requested kconfig symbols that did not"
            " survive the merge (F23)",
            f"{{indent}}{VERIFY_MARKER}() {{",
            "{indent}  local _frag _line _sym _want _got",
            "{indent}  for _frag in "
            + " ".join(f'"$startdir/{f}"' for f in fragments)
            + "; do",
            "{indent}    [ -f \"$_frag\" ] || continue",
            "{indent}    while IFS= read -r _line; do",
            "{indent}      case $_line in",
            "{indent}        CONFIG_*=*) _sym=${_line%%=*}; _want=${_line#*=} ;;",
            "{indent}        '# CONFIG_'*' is not set')"
            " _sym=${_line#\\# }; _sym=${_sym%% *}; _want=n ;;",
            "{indent}        *) continue ;;",
            "{indent}      esac",
            "{indent}      if _got=$(grep -m1 \"^$_sym=\" .config); then",
            "{indent}        _got=${_got#*=}",
            "{indent}      else",
            "{indent}        _got=n",
            "{indent}      fi",
            '{indent}      if [ "$_want" != "$_got" ]; then',
            "{indent}        printf '%s\\n' \"==> sysforge: WARNING: requested"
            " $_sym=$_want is not in the final .config (resolved to $_got)\" >&2",
            "{indent}      fi",
            "{indent}    done < \"$_frag\"",
            "{indent}  done",
            "{indent}}",
            f"{{indent}}{VERIFY_MARKER} || true",
        ),
        skip_if_present=VERIFY_MARKER,
    )


def _pause_lines(target: str) -> tuple[str, ...]:
    """The TTY-guarded pause that precedes an interactive kconfig review (B6).

    The pause sits in ``prepare()`` — after every merge has assembled the final
    ``.config``, immediately before the menu opens — because that is the only
    point that is genuinely "after all merges, before the editor". A
    stage-level pause before ``makepkg`` necessarily fires before these
    in-``prepare()`` merges run.

    Guarded so it is inert off a TTY (pipeline / captured stdin) and
    errexit-safe under makepkg's ``set -e``: ``read`` returns non-zero on EOF,
    swallowed by ``|| true``, and the ``if`` yields 0.
    """
    return (
        "{indent}if [ -t 0 ]; then",
        "{indent}  read -rp 'sysforge: merged kernel .config assembled — press "
        f"Enter to review it in {target} (Ctrl-C aborts)… ' _sf_kconfig_ack || true",
        "{indent}fi",
    )


def review_step() -> Step:
    """A TTY-guarded pause followed by the injected ``make nconfig`` (B6)."""
    return Step(
        slot=REVIEW,
        lines=(
            *_pause_lines("nconfig"),
            "{indent}make nconfig  # sysforge: interactive kconfig review",
        ),
    )


def ui_target_step(target: str) -> Step:
    """A configured UI tail split off :func:`generate_step` into REVIEW.

    Carries the same TTY-guarded :func:`_pause_lines` as the injected
    :func:`review_step` (2.6.1-F22). The pause originally shipped only with the
    injected ``nconfig``, on the reasoning that a target the operator named in
    ``kernel.toml`` needs no confirmation — but the pause is not a confirmation
    of the *target*, it is the operator's checkpoint on the assembled
    ``.config``, which is equally wanted however the review target got there.
    The message names the configured target rather than a hardcoded ``nconfig``.

    Carries a ``noninteractive_rewrite`` to :data:`OLDDEFCONFIG` — unlike the
    injected :func:`review_step`, a *configured* review target still has to
    resolve the config non-interactively when the run turns out unattended
    (old behaviour: ``patch_kconfig_targets`` wrote the tail, then
    ``patch_noninteractive_kconfig`` rewrote it in place).

    ``owns_generation=True``, same as :func:`generate_step` — the UI tail is
    still part of the configured sequence, not the injected review, so it
    must (a) trigger the removal pass even when it is the only configured
    step (a UI-only ``kconfig_targets``, :data:`GENERATE` left unfilled) and
    (b) survive the "packager already has a review target" drop rule, which
    exists only to suppress a second *injected* nconfig.
    """
    return Step(
        slot=REVIEW,
        lines=(*_pause_lines(target), "{indent}{make}" + target + "{trailer}"),
        noninteractive_rewrite=(f"{{indent}}{{make}}{OLDDEFCONFIG}{{trailer}}",),
        owns_generation=True,
    )
