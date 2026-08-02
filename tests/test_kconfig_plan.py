# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""Tests for the ordered kernel kconfig plan (2.5.1-F1)."""
import pytest

from sysforge.primitives import kconfig_plan as kp


class TestSlotOrder:
    """SLOT_ORDER is the sole ordering authority — assert its invariants
    directly, so a misordered registration fails here instead of silently
    corrupting a rendered prepare()."""

    def test_every_slot_is_ordered_exactly_once(self):
        slots = {kp.BASE_SEED, kp.FRAGMENT_MERGE, kp.GENERATE,
                 kp.HOTPLUG_MERGE, kp.REVIEW}
        assert set(kp.SLOT_ORDER) == slots
        assert len(kp.SLOT_ORDER) == len(slots)

    def test_base_seed_precedes_fragment_merge(self):
        # The base config is the .config the fragment overlays onto.
        assert kp.SLOT_ORDER.index(kp.BASE_SEED) < kp.SLOT_ORDER.index(kp.FRAGMENT_MERGE)

    def test_hotplug_merge_sits_between_generate_and_review(self):
        # After any minimizer (localmodconfig strips absent hardware), before
        # the operator reviews — this is the whole point of F2.
        assert (kp.SLOT_ORDER.index(kp.GENERATE)
                < kp.SLOT_ORDER.index(kp.HOTPLUG_MERGE)
                < kp.SLOT_ORDER.index(kp.REVIEW))

    def test_every_slot_has_exactly_one_region(self):
        assert set(kp.SLOT_REGION) == set(kp.SLOT_ORDER)
        assert set(kp.SLOT_REGION.values()) <= {kp.PRE, kp.POST}

    def test_regions_do_not_interleave(self):
        # Every PRE slot must sort before every POST slot, or the two splices
        # would render slots out of SLOT_ORDER.
        regions = [kp.SLOT_REGION[s] for s in kp.SLOT_ORDER]
        assert regions == sorted(regions, key=lambda r: 0 if r == kp.PRE else 1)


class TestContribution:
    def test_contribute_then_has(self):
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        assert plan.has(kp.BASE_SEED)
        assert not plan.has(kp.REVIEW)

    def test_filled_returns_slot_order_not_call_order(self):
        plan = kp.KconfigPlan()
        plan.contribute(kp.review_step())
        plan.contribute(kp.base_seed_step())
        assert plan.filled() == (kp.BASE_SEED, kp.REVIEW)

    def test_double_contribution_raises(self):
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        with pytest.raises(ValueError, match="already filled"):
            plan.contribute(kp.base_seed_step())

    def test_drop_removes_slot_and_is_idempotent(self):
        plan = kp.KconfigPlan()
        plan.contribute(kp.review_step())
        plan.drop(kp.REVIEW)
        plan.drop(kp.REVIEW)
        assert not plan.has(kp.REVIEW)

    def test_generate_step_renders_one_line_per_target(self):
        # {trailer} sits only on the LAST line — matching old
        # patch_kconfig_targets, which appended the captured trailer once,
        # after the whole joined block ("\n".join(...) + trailer), not once
        # per generated line.
        step = kp.generate_step(["olddefconfig", "localmodconfig"])
        assert step.slot == kp.GENERATE
        assert step.lines == (
            "{indent}{make}olddefconfig",
            "{indent}{make}localmodconfig{trailer}",
        )

    def test_owns_generation_steps_never_set_skip_if_present(self):
        # INVARIANT documented on Step.owns_generation: install()'s
        # removal-pass gate reads owns_generation on the POST-drop-rules step
        # dict, so an owns_generation step that also set skip_if_present
        # could have its marker-in-text idempotency drop silently disable the
        # removal pass along with it (the round-2 regression this field
        # exists to prevent). Neither current owns_generation builder may
        # regress this.
        for step in (kp.generate_step(["olddefconfig"]), kp.ui_target_step("menuconfig")):
            assert step.owns_generation
            assert step.skip_if_present is None

    def test_hotplug_step_is_guarded_by_its_fragment(self):
        # Idempotency: install() skips the slot when the text already
        # references the fragment.
        assert kp.hotplug_merge_step().skip_if_present == kp.HOTPLUG_FRAGMENT


# A stock kernel PKGBUILD: prepare() seeds .config and resolves it, with no
# fragment merge and no UI target of its own.
STOCK = """\
pkgname=linux-custom
prepare() {
  cd "$srcdir/linux"
  cp ../config .config
  make olddefconfig
}
"""

# A PKGBUILD that minimizes and then opens a menu of its own.
MINIMIZE_THEN_MENU = """\
pkgname=linux-custom
prepare() {
  cd "$srcdir/linux"
  make ARCH=x86_64 localmodconfig  # trim
  make menuconfig
}
"""

# A PKGBUILD that only minimizes — no packager-owned review target. Used for
# the UI-only configured kconfig_targets regression (2.5.1-F1 round 2): the
# whole point is that this single packager-owned line must still be removed
# even though the configured sequence is entirely a UI tail (GENERATE itself
# ends up unfilled).
MINIMIZE_ONLY = """\
pkgname=linux-custom
prepare() {
  cd "$srcdir/linux"
  make ARCH=x86_64 localmodconfig  # trim
}
"""


# A PKGBUILD whose review target (menuconfig) precedes its kconfig-resolve
# line (olddefconfig) — the POST anchor (menuconfig, review) sorts BEFORE the
# PRE anchor (olddefconfig, setup) in the text, unlike every other fixture
# above where PRE precedes POST. Used to reproduce the post_at < pre_at splice
# ordering bug.
MENU_THEN_OLDDEFCONFIG = """\
pkgname=linux-custom
prepare() {
  cd "$srcdir/linux"
  make menuconfig
  make olddefconfig
}
"""


def _write(tmp_path, text):
    p = tmp_path / "PKGBUILD.sysforge"
    p.write_text(text, encoding="utf-8")
    return p


def _full_plan(targets=None, *, review=True):
    """The plan makepkg_wrapper builds for a kernel run."""
    plan = kp.KconfigPlan()
    plan.contribute(kp.base_seed_step())
    plan.contribute(kp.fragment_merge_step())
    if targets:
        plan.contribute(kp.generate_step(targets))
    plan.contribute(kp.hotplug_merge_step())
    if review:
        plan.contribute(kp.review_step())
    return plan


class TestInstallRendersInSlotOrder:
    def test_generate_case_renders_one_contiguous_block(self, tmp_path):
        p = _write(tmp_path, STOCK)
        # localmodconfig, not olddefconfig: the base-seed and fragment blocks
        # each emit their own `make olddefconfig`, so that target cannot
        # identify the GENERATE slot in an ordering assertion.
        _full_plan(["localmodconfig"], review=False).install(p, noninteractive=True)
        out = p.read_text()
        assert out.index("sysforge.base.config") < out.index('"$startdir/sysforge.config"') \
            < out.index("make localmodconfig") < out.index(kp.HOTPLUG_FRAGMENT)

    def test_packager_resolve_line_is_removed_when_generate_is_filled(self, tmp_path):
        p = _write(tmp_path, STOCK)
        _full_plan(["localmodconfig"], review=False).install(p, noninteractive=True)
        # The packager's own `make olddefconfig` is gone; every surviving one
        # belongs to a rendered guard block.
        assert "  make olddefconfig\n}" not in p.read_text()

    def test_review_renders_last(self, tmp_path):
        p = _write(tmp_path, STOCK)
        _full_plan(["olddefconfig"]).install(p, noninteractive=False)
        out = p.read_text()
        assert out.index(kp.HOTPLUG_FRAGMENT) < out.index("make nconfig")
        assert out.index("_sf_kconfig_ack") < out.index("make nconfig")

    def test_full_post_sequence_generate_hotplug_review_order(self, tmp_path):
        # Pins an accepted reordering vs. today's pkgbuild_patcher.py: the
        # hotplug merge now renders before the review pause (old code
        # rendered it after), because the pause text promises "merged kernel
        # .config assembled", which is only true once the hotplug merge has
        # actually run. Must fail under the old (hotplug-after-pause) order.
        p = _write(tmp_path, STOCK)
        _full_plan(["localmodconfig"]).install(p, noninteractive=False)
        out = p.read_text()
        assert (out.index("make localmodconfig")
                < out.index(kp.HOTPLUG_FRAGMENT)
                < out.index("_sf_kconfig_ack")
                < out.index("make nconfig"))

    def test_indent_taken_from_replaced_line(self, tmp_path):
        p = _write(tmp_path, STOCK)
        _full_plan(["olddefconfig"]).install(p, noninteractive=False)
        assert '  if [ -f "$startdir/sysforge.base.config" ]; then' in p.read_text()

    def test_var_args_and_trailing_comment_preserved(self, tmp_path):
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(["localmodconfig"]).install(p, noninteractive=True)
        assert "make ARCH=x86_64 localmodconfig  # trim" in p.read_text()

    def test_multi_target_trailer_appears_once(self, tmp_path):
        # Old patch_kconfig_targets appended the captured trailer ONCE, after
        # the whole joined block ("\n".join(...) + trailer) — not once per
        # generated line. A multi-target sequence against an anchor line
        # carrying a trailing comment must not repeat that comment.
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(["localmodconfig", "olddefconfig"], review=False).install(
            p, noninteractive=True)
        out = p.read_text()
        assert out.count("# trim") == 1
        assert "make ARCH=x86_64 localmodconfig\n" in out
        assert "make ARCH=x86_64 olddefconfig  # trim" in out

    def test_trailer_appears_once_with_generate_and_configured_ui_tail(self, tmp_path):
        # GENERATE and a configured UI tail (split into REVIEW by
        # ui_target_step) both carry the {trailer} token in their own
        # lines() tuple — each written assuming it's the sole POST
        # contributor using it. Both land in POST when kconfig_targets ends
        # in a UI target, so without dedup the captured anchor trailer
        # renders twice (once on GENERATE's last line, again on REVIEW's).
        # The old patch_kconfig_targets appended the trailer exactly once,
        # on the last line overall — here that's the UI tail.
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        plan = kp.KconfigPlan()
        plan.contribute(kp.generate_step(["localmodconfig", "olddefconfig"]))
        plan.contribute(kp.ui_target_step("menuconfig"))
        plan.install(p, noninteractive=False)
        out = p.read_text()
        assert out.count("# trim") == 1
        assert "make ARCH=x86_64 olddefconfig\n" in out
        assert "make ARCH=x86_64 menuconfig  # trim" in out


class TestNoGenerateSlot:
    """With no configured targets the packager's kconfig steps survive, and
    the two regions must straddle them."""

    def test_seed_and_hotplug_render_after_minimizer_before_menu(self, tmp_path):
        # No GENERATE slot: install() anchors on the packager's own
        # localmodconfig line (the sole kconfig-resolve line in the text) and
        # renders both regions immediately after it, ahead of the packager's
        # own menuconfig — matching today's pkgbuild_patcher.py ordering
        # (verified against patch_kernel_kconfig_apply / _hotplug_fragment_merge).
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(None, review=False).install(p, noninteractive=False)
        out = p.read_text()
        assert (out.index("localmodconfig")
                < out.index("sysforge.base.config")
                < out.index(kp.HOTPLUG_FRAGMENT)
                < out.index("make menuconfig"))

    def test_packager_kconfig_lines_are_not_removed(self, tmp_path):
        # The packager's own seed + resolve lines survive verbatim and
        # adjacent (nothing spliced between them) — only what follows them
        # changes, matching today's pkgbuild_patcher.py behaviour.
        p = _write(tmp_path, STOCK)
        _full_plan(None, review=False).install(p, noninteractive=True)
        assert '  cp ../config .config\n  make olddefconfig\n' in p.read_text()

    def test_post_indent_pinned_at_anchor_line_indent_not_column_zero(self, tmp_path):
        # Accepted deviation from pkgbuild_patcher.py's
        # patch_hotplug_fragment_merge, which read the indent of the line
        # *after* the insertion point — for STOCK that's the closing `}` at
        # column 0. install() instead uses the matched anchor line's own
        # 2-space indent, rendering a properly-nested guard block. Must fail
        # if the indent regresses to column 0.
        p = _write(tmp_path, STOCK)
        _full_plan(None, review=False).install(p, noninteractive=True)
        out = p.read_text()
        assert f'  if [ -f "$startdir/{kp.HOTPLUG_FRAGMENT}" ]; then' in out
        assert f'\nif [ -f "$startdir/{kp.HOTPLUG_FRAGMENT}" ]; then' not in out


class TestSpliceOrderWithPostBeforePre:
    """When no GENERATE slot is filled, the POST anchor (last review/kconfig
    line) and the PRE anchor (first kconfig-resolve line) are derived
    independently and nothing guarantees post_at >= pre_at. install() must
    splice by actual offset, not assume PRE always precedes POST in the
    text — a wrong assumption there corrupts prepare() by inserting PRE at a
    stale offset inside the already-inserted POST block."""

    def test_post_before_pre_renders_well_formed_prepare(self, tmp_path):
        p = _write(tmp_path, MENU_THEN_OLDDEFCONFIG)
        _full_plan(None, review=False).install(p, noninteractive=False)
        out = p.read_text()
        # The hotplug comment must survive intact — the bug split it
        # mid-line, leaving a bare `s as modules after minimization (F2)`
        # command that aborts prepare() under `set -e`.
        assert ("# sysforge: re-enable hotplug drivers as modules after "
                "minimization (F2)") in out
        assert "s as modules after minimization (F2)" not in out.replace(
            "# sysforge: re-enable hotplug drivers as modules after "
            "minimization (F2)", "")
        # The packager's own two lines must survive intact and adjacent —
        # neither corrupted nor separated by a spliced-in block. (Both
        # generate_step's and hotplug_merge_step's own rendered bodies also
        # contain the substring "make olddefconfig", so this exact adjacent
        # two-line landmark — not a bare .index("make olddefconfig") — is
        # what pins "packager's own lines, untouched".)
        packager_lines = "  make menuconfig\n  make olddefconfig\n"
        assert packager_lines in out
        # POST (hotplug) renders before the menuconfig line it reviews ahead
        # of; PRE (base_seed/fragment_merge) renders after the olddefconfig
        # resolve line — and the whole POST block must not have PRE spliced
        # into its middle.
        post_block_start = out.index(kp.HOTPLUG_FRAGMENT)
        packager_start = out.index(packager_lines)
        pre_block_start = out.index("sysforge.base.config")
        assert post_block_start < packager_start
        assert pre_block_start > packager_start + len(packager_lines)
        assert "sysforge.base.config" not in out[post_block_start:packager_start]
        assert '"$startdir/sysforge.config"' not in out[post_block_start:packager_start]


class TestDropRules:
    def test_cooperating_pkgbuild_keeps_generate_and_hotplug(self, tmp_path):
        text = STOCK.replace(
            "  make olddefconfig\n",
            '  ./scripts/kconfig/merge_config.sh -m .config "$startdir/x.config"\n'
            "  make olddefconfig\n")
        p = _write(tmp_path, text)
        _full_plan(["olddefconfig"], review=False).install(p, noninteractive=True)
        out = p.read_text()
        assert "sysforge.base.config" not in out          # PRE dropped
        assert out.count("sysforge.config\"") == 0        # PRE dropped
        assert kp.HOTPLUG_FRAGMENT in out                 # POST survives

    def test_hotplug_slot_drops_when_its_fragment_is_already_referenced(self, tmp_path):
        # Slot-level idempotency. `install` as a whole is deliberately not
        # idempotent — it is a render-once operation on a PKGBUILD.sysforge
        # that the kernel stage regenerates per build, and a second removal
        # pass would eat the lines the first pass rendered.
        text = STOCK.replace(
            "  make olddefconfig\n",
            f'  ./scripts/kconfig/merge_config.sh -m .config "$startdir/{kp.HOTPLUG_FRAGMENT}"\n'
            "  make olddefconfig\n")
        p = _write(tmp_path, text)
        plan = kp.KconfigPlan()
        plan.contribute(kp.hotplug_merge_step())
        plan.install(p, noninteractive=True)
        assert p.read_text().count(kp.HOTPLUG_FRAGMENT) == 1

    def test_packager_review_target_suppresses_injected_nconfig(self, tmp_path):
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(None).install(p, noninteractive=False)
        out = p.read_text()
        assert "make nconfig" not in out
        assert "_sf_kconfig_ack" not in out
        assert "make menuconfig" in out

    def test_oldconfig_does_not_suppress_injected_nconfig(self, tmp_path):
        # oldconfig is a non-interactive *resolve*, not an operator review.
        p = _write(tmp_path, STOCK.replace("make olddefconfig", "make oldconfig"))
        _full_plan(None).install(p, noninteractive=False)
        assert "make nconfig" in p.read_text()


class TestNonInteractive:
    def test_drops_injected_review(self, tmp_path):
        # The *injected* review_step() (TTY pause + nconfig) has no
        # noninteractive_rewrite — unattended run drops it outright, unlike a
        # configured UI tail (see test_rewrites_configured_ui_tail_to_olddefconfig).
        p = _write(tmp_path, STOCK)
        _full_plan(["olddefconfig"]).install(p, noninteractive=True)
        out = p.read_text()
        assert "make nconfig" not in out
        assert "_sf_kconfig_ack" not in out

    def test_rewrites_configured_ui_tail_to_olddefconfig(self, tmp_path):
        # A *configured* review target (unlike the injected review_step) must
        # still resolve the config non-interactively — rewritten to
        # olddefconfig, not dropped outright (old behaviour: patch_kconfig_targets
        # wrote the tail, then patch_noninteractive_kconfig rewrote it in place).
        p = _write(tmp_path, STOCK)
        plan = kp.KconfigPlan()
        plan.contribute(kp.generate_step(["localmodconfig"]))
        plan.contribute(kp.ui_target_step("menuconfig"))
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert "make localmodconfig" in out
        assert "make menuconfig" not in out
        assert "make olddefconfig" in out

    def test_rewrites_surviving_packager_ui_target(self, tmp_path):
        # No GENERATE slot, so no removal pass: the packager's menuconfig
        # survives and must be rewritten for an unattended run.
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(None, review=False).install(p, noninteractive=True)
        out = p.read_text()
        assert "make menuconfig" not in out
        assert "make olddefconfig" in out

    def test_rewrite_preserves_var_args_and_comment(self, tmp_path):
        p = _write(tmp_path, STOCK.replace(
            "  make olddefconfig", "  make ARCH=x86_64 nconfig  # review"))
        _full_plan(None, review=False).install(p, noninteractive=True)
        assert "make ARCH=x86_64 olddefconfig  # review" in p.read_text()

    def test_rewrite_is_a_noop_after_a_removal_pass(self, tmp_path):
        # The invariant install() relies on: removal deletes every kconfig
        # line, so no UI target can survive to be rewritten (and therefore no
        # rewrite can shift the recorded anchor offset).
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        _full_plan(["localmodconfig"], review=False).install(p, noninteractive=True)
        out = p.read_text()
        assert out.count("localmodconfig") == 1
        assert "menuconfig" not in out


class TestConfiguredUiTailOwnsGeneration:
    """Regressions from 4099308's UI-tail split: a configured kconfig_targets
    sequence that is entirely (or partly) a UI target must behave exactly
    like a non-UI configured sequence for removal-pass and drop-rule
    purposes — the split into REVIEW must not change who owns generation."""

    def test_ui_only_target_removes_packager_line_and_preserves_var_and_comment(
        self, tmp_path,
    ):
        # kconfig_targets=["menuconfig"] alone: the whole configured sequence
        # is the UI tail, so GENERATE itself never gets filled — but the
        # packager's own `make ARCH=x86_64 localmodconfig  # trim` must still
        # be removed (old patch_kconfig_targets removed it unconditionally
        # for any non-empty targets) and the rendered tail must reuse its
        # VAR=val prefix and trailing comment, not the generic "make "/"".
        p = _write(tmp_path, MINIMIZE_ONLY)
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        plan.contribute(kp.fragment_merge_step())
        plan.contribute(kp.hotplug_merge_step())
        plan.contribute(kp.ui_target_step("menuconfig"))
        plan.install(p, noninteractive=False)
        out = p.read_text()
        assert "make ARCH=x86_64 localmodconfig  # trim" not in out
        assert "make ARCH=x86_64 menuconfig  # trim" in out

    def test_configured_tail_survives_when_packager_already_has_a_review_target_interactive(
        self, tmp_path,
    ):
        # kconfig_targets=["localmodconfig", "menuconfig"] against a PKGBUILD
        # that already has its own `make menuconfig`: the "packager already
        # opens a review menu" drop rule must not eat the CONFIGURED tail —
        # pass 2 removes the packager's own menu line as part of the
        # configured sequence's removal, so the configured tail is the only
        # review left, not zero.
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        plan.contribute(kp.fragment_merge_step())
        plan.contribute(kp.hotplug_merge_step())
        plan.contribute(kp.generate_step(["localmodconfig"]))
        plan.contribute(kp.ui_target_step("menuconfig"))
        plan.install(p, noninteractive=False)
        out = p.read_text()
        assert out.count("menuconfig") == 1
        assert "make ARCH=x86_64 menuconfig  # trim" in out

    def test_configured_tail_rewritten_not_dropped_noninteractive(self, tmp_path):
        # Same PKGBUILD/plan as above but noninteractive=True: the configured
        # tail must be rewritten to olddefconfig (matching old
        # patch_kconfig_targets + patch_noninteractive_kconfig composition),
        # not silently dropped by the "packager already has a review" rule.
        p = _write(tmp_path, MINIMIZE_THEN_MENU)
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        plan.contribute(kp.fragment_merge_step())
        plan.contribute(kp.hotplug_merge_step())
        plan.contribute(kp.generate_step(["localmodconfig"]))
        plan.contribute(kp.ui_target_step("menuconfig"))
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert "menuconfig" not in out
        assert "make ARCH=x86_64 olddefconfig  # trim" in out
        # base_seed + fragment_merge + hotplug_merge each render one
        # unconditional plain "make olddefconfig" (3, no VAR=/trailer — they
        # don't reuse the removed line's make_prefix/trailer) regardless of
        # REVIEW; a 4th "olddefconfig" occurrence — the
        # "ARCH=x86_64 olddefconfig  # trim" one above, reusing the removed
        # line's VAR= prefix and trailing comment — must come from the
        # configured tail's rewrite, or this assertion can't distinguish
        # "rewritten" from "silently dropped" (both leave plain
        # "make olddefconfig" somewhere in the output via the other three
        # steps).
        assert out.count("olddefconfig") == 4


class TestFailureModes:
    def test_generate_without_any_packager_kconfig_line_raises(self, tmp_path):
        p = _write(tmp_path, "prepare() {\n  cd src\n}\n")
        plan = kp.KconfigPlan()
        plan.contribute(kp.generate_step(["olddefconfig"]))
        with pytest.raises(RuntimeError, match="no kconfig make invocation"):
            plan.install(p, noninteractive=True)

    def test_no_anchor_at_all_leaves_file_untouched(self, tmp_path):
        text = "prepare() {\n  cd src\n}\n"
        p = _write(tmp_path, text)
        _full_plan(None, review=False).install(p, noninteractive=True)
        assert p.read_text() == text

    def test_empty_plan_leaves_file_untouched(self, tmp_path):
        p = _write(tmp_path, STOCK)
        kp.KconfigPlan().install(p, noninteractive=True)
        assert p.read_text() == STOCK


class TestPortedFromPatcher:
    """Ported from the pre-refactor tests/test_patcher.py, which exercised
    these behaviours against the now-deleted patch_noninteractive_kconfig /
    patch_kconfig_targets / patch_kernel_kconfig_apply / patch_hotplug_
    fragment_merge (2.5.1-F1 Task 4). Each test here drives the equivalent
    KconfigPlan steps instead of calling a patch_* function directly."""

    def test_generate_emits_exactly_one_line_per_target(self, tmp_path):
        p = _write(tmp_path, STOCK)
        targets = ["olddefconfig", "localmodconfig", "savedefconfig"]
        plan = kp.KconfigPlan()
        plan.contribute(kp.generate_step(targets))
        plan.install(p, noninteractive=True)
        lines = [ln.strip() for ln in p.read_text().splitlines()]
        # Line-oriented, not a summed substring count: a target rendered
        # twice while another renders zero times must not average out.
        for t in targets:
            assert lines.count(f"make {t}") == 1

    def test_base_seed_is_file_guarded_and_precedes_the_fragment(self, tmp_path):
        p = _write(tmp_path, STOCK)
        plan = kp.KconfigPlan()
        plan.contribute(kp.base_seed_step())
        plan.contribute(kp.fragment_merge_step())
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert 'if [ -f "$startdir/sysforge.base.config" ]; then' in out
        assert 'cp "$startdir/sysforge.base.config" .config' in out
        # The cp appears ONLY inside the guard — an unconditional copy
        # anywhere else in the file must fail this.
        assert out.count('cp "$startdir/sysforge.base.config" .config') == 1
        assert out.index("sysforge.base.config") < out.index('"$startdir/sysforge.config"')

    def test_config_seed_line_anchors_when_no_resolve_step_exists(self, tmp_path):
        # A PKGBUILD that seeds .config with cp but never runs a make-config
        # target: _CONFIG_WRITE_RE is the fallback anchor.
        p = _write(tmp_path, "prepare() {\n  cp ../config .config\n}\n")
        plan = kp.KconfigPlan()
        plan.contribute(kp.fragment_merge_step())
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert out.index("cp ../config .config") < out.index("merge_config.sh")

    @pytest.mark.parametrize(
        "target",
        ["config", "nconfig", "menuconfig", "xconfig", "gconfig", "oldconfig"])
    def test_every_ui_target_is_stripped_on_an_unattended_run(self, tmp_path, target):
        # Covers the old parametrized _strips_every_ui_target *and*
        # _strips_bare_config (the "config" case) in one sweep. `oldconfig` is
        # the B17 seam: it's in _INTERACTIVE_KCONFIG_RE's alternation (so its
        # consumer — this rewrite — must turn it into olddefconfig) but
        # deliberately excluded from _REVIEW_KCONFIG_RE (a bare non-interactive
        # resolve step must not suppress the injected nconfig review). Without
        # this case, deleting "oldconfig" from _INTERACTIVE_KCONFIG_RE's
        # alternation is undetected by this suite.
        p = _write(tmp_path, STOCK.replace("make olddefconfig", f"make {target}"))
        plan = kp.KconfigPlan()
        plan.contribute(kp.hotplug_merge_step())
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert f"make {target}" not in out
        assert "make olddefconfig" in out

    def test_olddefconfig_is_left_intact(self, tmp_path):
        p = _write(tmp_path, STOCK)
        plan = kp.KconfigPlan()
        plan.contribute(kp.hotplug_merge_step())
        plan.install(p, noninteractive=True)
        assert p.read_text().count("make olddefconfig") == 2  # packager's + hotplug's

    def test_olddefconfig_is_not_half_matched_by_the_ui_rewrite(self, tmp_path):
        # Adding bare `config` to the interactive-target alternation must not
        # half-match `olddefconfig` (or any other non-interactive target).
        text = "prepare() {\n  make olddefconfig\n  make localmodconfig\n}\n"
        p = _write(tmp_path, text)
        plan = kp.KconfigPlan()
        plan.contribute(kp.hotplug_merge_step())
        plan.install(p, noninteractive=True)
        assert "  make olddefconfig\n  make localmodconfig\n" in p.read_text()

    def test_pre_region_indent_matches_a_tab_indented_anchor(self, tmp_path):
        # The PRE anchor's own indentation (a tab here) is reused verbatim —
        # distinct from the POST-region indent-taken-from-anchor coverage in
        # TestInstallRendersInSlotOrder, which only exercises 2-space STOCK.
        p = _write(tmp_path, "prepare() {\n\tmake olddefconfig\n}\n")
        plan = kp.KconfigPlan()
        plan.contribute(kp.fragment_merge_step())
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert '\n\tif [ -f "$startdir/sysforge.config" ]; then' in out

    def test_review_pause_is_tty_guarded_and_errexit_safe(self, tmp_path):
        # B6: the injected pause must be inert off a TTY (pipeline / captured
        # stdin builds) and must not abort under makepkg's `set -e` — `read`
        # returns non-zero on EOF, so it needs `|| true`.
        p = _write(tmp_path, STOCK)
        plan = kp.KconfigPlan()
        plan.contribute(kp.review_step())
        plan.install(p, noninteractive=False)
        out = p.read_text()
        assert "if [ -t 0 ]; then" in out
        assert "read -rp" in out
        assert "|| true" in out

    def test_non_kconfig_make_lines_survive_removal_and_rewrite(self, tmp_path):
        # Neither the owns_generation removal pass (_ANY_KCONFIG_RE) nor the
        # non-interactive UI rewrite (_INTERACTIVE_KCONFIG_RE) may eat a make
        # invocation whose target isn't a kconfig target at all — both regexes
        # anchor on the whitelisted target alternation, and this line's
        # target ("all" / "modules_install") is deliberately not in it.
        text = (
            "prepare() {\n"
            "  cd $_srcname\n"
            "  cp ../config.$CARCH .config\n"
            "  make olddefconfig\n"
            "  make LOCALVERSION=v1 all\n"
            "  make modules_install\n"
            "}\n"
        )
        p = _write(tmp_path, text)
        plan = kp.KconfigPlan()
        plan.contribute(kp.generate_step(["localmodconfig"]))
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert "make LOCALVERSION=v1 all" in out
        assert "make modules_install" in out

    def test_multiple_ui_targets_in_one_file_are_all_rewritten(self, tmp_path):
        text = (
            "prepare() {\n"
            "  cd $_srcname\n"
            "  cp ../config.$CARCH .config\n"
            "  make olddefconfig\n"
            "  make menuconfig\n"
            "  make nconfig\n"
            "}\n"
        )
        p = _write(tmp_path, text)
        plan = kp.KconfigPlan()
        plan.contribute(kp.hotplug_merge_step())
        plan.install(p, noninteractive=True)
        out = p.read_text()
        assert "menuconfig" not in out
        assert "nconfig" not in out
