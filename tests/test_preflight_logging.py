"""Pre-flight report blocks must reach the unified run-log (2.6.1-F10).

Both pre-flights (LLVM source state, toolchain availability) render a block a
user re-reads later to understand why a run behaved as it did. Emitted with a
bare ``print()`` they reach the terminal only — ``log.ui`` is the one home that
mirrors UI output into the run-log and applies ``downgrade_glyphs``.
"""

from pathlib import Path
from unittest.mock import patch

from sysforge import log
from sysforge.primitives.llvm_state import LlvmPackageState, LlvmPreflightReport
from sysforge.primitives.toolchain_preflight import (
    ToolchainCheck,
    ToolchainPreflightReport,
)


def _state() -> LlvmPackageState:
    """A dirty, foreign-origin llvm — actionable, so it survives the filter."""
    return LlvmPackageState(
        pkgbase="llvm", pkgbuild_dir=Path("/src/llvm"), variant="llvm",
        source_origin="aur", remote_url=None, is_dirty=True,
        dirty_reason="uncommitted changes", divergence="up_to_date",
        head_short=None, upstream_short=None, install_origin="foreign",
        installed_ver="20.1.0", pkgbuild_ver="20.1.0", build_mode="source_built",
        pgo_profdata_mismatch=False,
    )


def _report(*states) -> LlvmPreflightReport:
    return LlvmPreflightReport(
        states=tuple(states), blockers=(),
        has_dirty=any(s.is_dirty for s in states),
        has_diverged=any(s.divergence == "diverged" for s in states),
        has_pgo_profdata_mismatch=any(s.pgo_profdata_mismatch for s in states),
    )


def _capture_log(tmp_path, fn) -> str:
    """Run fn() with a unified log open and return the log's contents."""
    path = tmp_path / "sysforge.log"
    log.open_unified_log(path)
    try:
        fn()
    finally:
        log.close_unified_log(success=False, persist=True)
    return path.read_text()


def test_build_llvm_preflight_reaches_unified_log(tmp_path):
    from sysforge import build_cmd

    with patch("sysforge.primitives.llvm_state.collect_llvm_state",
               return_value=_report(_state())):
        content = _capture_log(
            tmp_path, lambda: build_cmd._render_llvm_preflight(["llvm"], {})
        )

    assert "LLVM source pre-flight" in content


def _check(ok: bool) -> ToolchainCheck:
    return ToolchainCheck(
        name="rust:stable", ok=ok,
        detail="present" if ok else "not installed",
        fix_cmd="rustup toolchain install stable", auto_remediable=True,
    )


class _Args:
    non_interactive = True
    noconfirm = False


def _run_toolchain_preflight(tmp_path, report: ToolchainPreflightReport) -> str:
    """Drive update's toolchain pre-flight with the rustup probes stubbed out.

    ``collect_required_toolchains`` and ``run_toolchain_preflight`` shell out;
    the renderer whose output we're tracking is the real one.
    """
    from sysforge import update

    with patch("sysforge.update.collect_required_toolchains",
               return_value=("rust:stable",)), \
            patch("sysforge.update.run_toolchain_preflight", return_value=report), \
            patch("sysforge.update.auto_remediate_toolchain", return_value=report):
        return _capture_log(
            tmp_path,
            lambda: update._toolchain_preflight_for_batch([], {}, _Args()),
        )


def test_update_toolchain_preflight_reaches_unified_log(tmp_path):
    content = _run_toolchain_preflight(
        tmp_path, ToolchainPreflightReport(checks=(_check(ok=True),))
    )
    assert "Toolchain pre-flight" in content
    assert "rust:stable" in content


def test_update_failed_toolchain_preflight_reaches_unified_log(tmp_path):
    """The failure block is the one most worth having in the log."""
    content = _run_toolchain_preflight(
        tmp_path, ToolchainPreflightReport(checks=(_check(ok=False),))
    )
    assert "FAIL" in content
    assert "rustup toolchain install stable" in content


def test_no_preflight_block_is_emitted_with_bare_print():
    """Structural guard for the sites a unit test can't reach.

    ``fetch.cmd_fetch`` and ``update``'s Phase 1.5 render their block inline in
    a long command function. Rather than drive those whole functions, assert at
    the source level that no module hands a pre-flight render to ``print()``.
    """
    import re

    root = Path(__file__).resolve().parent.parent / "sysforge"
    pattern = re.compile(r"print\(\s*render_\w*preflight\b")
    offenders = [
        f"{p.relative_to(root)}:{i}"
        for p in root.rglob("*.py")
        for i, line in enumerate(p.read_text().splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == [], (
        "pre-flight blocks must route through log.ui, not print(): "
        + ", ".join(offenders)
    )
