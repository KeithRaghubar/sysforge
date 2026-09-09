# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/bolt.py — Pass 5, BOLT post-link optimization.

Strictly optional and strictly last: BOLT rewrites the *already installed* PGO
clang binary, so it runs after the suite is installed and verified, and a
failure here leaves a working toolchain rather than a broken one.

Separate from ``pgo.py`` because it is a different tool with a different
failure mode — the four PGO passes are one interdependent sequence, while this
either improves the result or is skipped.
"""
from pathlib import Path
import contextlib

from sysforge.pipeline.stages.toolchain import config, passes, verify

from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# BOLT Pass 5 (post-link optimization of the just-installed PGO clang)
# ---------------------------------------------------------------------------


def build_bolt_tools(
    tcfg: "config.ToolchainConfig", sysforge_config: dict, options,
    variant: str | None,
) -> bool:
    """Pass 5a — build+install the BOLT tools (`llvm-bolt`/`perf2bolt`/…).

    BOLT is EXPERIMENTAL: it is not in the official Arch repos and the stock
    `llvm` package does not build it. sysforge generates an `llvm-bolt` PKGBUILD
    (`bolt.materialize_pkgbuild`, version-locked to the just-installed llvm) and
    builds it standalone against the installed PGO libLLVM — the `bolt/` subtree
    rides inside the same `llvm-project` monorepo tarball the `llvm` build used.

    Returns True if the tools are available afterward (built now, or already
    present). Best-effort: a build failure WARNs and returns False so Pass 5b is
    skipped and the verified PGO toolchain is left intact.
    """
    from sysforge.primitives import bolt as _bolt

    # Already present (e.g. a prior run installed them) — nothing to build.
    if _bolt.tools_available(need_perf=False)[0]:
        return True

    # BLOCKED guard: BOLT's tools all force static LLVM linkage
    # (DISABLE_LLVM_LINK_LLVM_DYLIB), so a standalone build needs the per-component
    # static archives. The PGO toolchain (like stock Arch llvm) is dylib-only and
    # omits them — the build would fail ~100 ninja steps in with an unfindable
    # -lLLVMObject. Fail fast with the reason instead of a cryptic late link error.
    if not _bolt.standalone_build_viable():
        _log.warn(
            "[BOLT] Pass 5 skipped — BLOCKED: the BOLT tools link LLVM statically, but "
            "the PGO toolchain ships a dylib-only libLLVM without the per-component "
            "static archives a standalone llvm-bolt build needs. Building BOLT in-tree "
            "with LLVM is the only viable path (not yet implemented). PGO toolchain left "
            "intact — see DESIGN.md §Toolchain stage (BOLT Pass 5)."
        )
        return False

    pkgbuild_dir = (sysforge_config.get("paths", {}) or {}).get("pkgbuild_src_dir")
    if not pkgbuild_dir:
        _log.warn(
            "[BOLT] Pass 5a skipped — no [paths] pkgbuild_src_dir to materialize "
            "the llvm-bolt PKGBUILD into; PGO toolchain left as-is."
        )
        return False

    llvm_ver = verify.query_pacman_versions(("llvm",)).get("llvm")
    if not llvm_ver:
        _log.warn("[BOLT] Pass 5a skipped — installed llvm version not found")
        return False
    pkgver = llvm_ver.split("-", 1)[0]  # strip pkgrel; BOLT locks to llvm pkgver

    try:
        pkgbuild = _bolt.materialize_pkgbuild(Path(pkgbuild_dir), pkgver)
    except OSError as e:
        _log.warn(f"[BOLT] Pass 5a skipped — could not write llvm-bolt PKGBUILD ({e})")
        return False

    _log.ui(
        f"[BOLT] Pass 5a — building the BOLT tools (llvm-bolt {pkgver}, "
        "experimental: no official Arch package) against the installed libLLVM"
    )
    try:
        passes.build_pkg(
            _bolt.PKG_NAME, pkgbuild, options,
            extra_flags=["--install"],
            toolchain_variant=variant,
            owner_stage="toolchain",
        )
    except Exception as e:
        _log.warn(
            f"[BOLT] Pass 5a failed to build llvm-bolt ({e}) — PGO toolchain "
            "left in place; BOLT optimization skipped."
        )
        return False

    ok, missing = _bolt.tools_available(need_perf=False)
    if not ok:
        _log.warn(
            f"[BOLT] llvm-bolt built but {', '.join(missing)} still not on PATH — "
            "skipping BOLT optimization."
        )
    return ok


def run_bolt(
    tcfg: "config.ToolchainConfig", sysforge_config: dict, options,
    variant: str | None,
) -> None:
    """Pass 5 — BOLT-optimize the freshly-installed PGO clang (post-link).

    Runs after Gate 3 has *verified* the PGO toolchain in ``/usr`` and inside the
    stage sentinel, so a mishap is covered by the same snapshot rollback as the
    install. The canonical PGO→BOLT "fast clang" stack, in two steps: **4a**
    builds the BOLT tools sysforge needs (see :func:`build_bolt_tools` — they are
    not in the Arch repos), then **4b** profiles the installed clang on a
    representative compile job (``perf record``), converts with ``perf2bolt``,
    rewrites with ``llvm-bolt``, smoke-tests the result, and only then atomically
    replaces ``/usr/bin/clang``.

    EXPERIMENTAL and best-effort throughout: a failed tool build, a missing
    ``perf``, a ``perf``/``llvm-bolt`` failure, or a failed smoke test WARNs and
    leaves the verified PGO clang untouched — BOLT is an opt-in extra win, never
    allowed to regress the working toolchain. No-op unless ``[bolt] enabled`` (and
    not dry-run). Note: this rewrites the installed binary post-link, so
    ``pacman -Qkk clang`` will report it modified — an inherent property of
    post-link optimization, not corruption.
    """
    import subprocess as _sp
    import tempfile as _tempfile

    bcfg = tcfg.bolt
    if not bcfg.enabled:
        return
    if options.dry_run:
        _log.ui(
            "[dry-run] would build the BOLT tools (experimental) and "
            "BOLT-optimize the installed clang (Pass 5)"
        )
        return

    from sysforge.primitives import bolt as _bolt
    from sysforge.primitives import fs_provision as _fsp

    # Pass 5a — sysforge builds llvm-bolt/perf2bolt itself (not in Arch repos).
    if not build_bolt_tools(tcfg, sysforge_config, options, variant):
        return

    # perf (linux-tools) is needed for collection but isn't something sysforge
    # builds — surface its absence with an actionable hint, don't silently skip.
    ok, missing = _bolt.tools_available(need_perf=True)
    if not ok:
        _log.warn(
            f"[BOLT] Pass 5b skipped — {', '.join(missing)} not on PATH "
            "(install the linux-tools `perf` package). PGO clang left in place."
        )
        return

    clang = Path("/usr/bin/clang")
    clangxx = Path("/usr/bin/clang++")
    if not clang.exists():
        _log.warn("[BOLT] Pass 5 skipped — /usr/bin/clang not found")
        return

    store = _bolt.resolve_store(tcfg)
    try:
        _fsp.ensure_writable_dir(store)
    except _fsp.FsProvisionError as e:
        _log.warn(f"[BOLT] profile store {store} not group-provisioned ({e})")

    with _tempfile.TemporaryDirectory(prefix="sysforge-bolt-") as _td:
        td = Path(_td)
        workload_cfg = bcfg.training_workload
        workload = (
            Path(workload_cfg).expanduser()
            if workload_cfg
            else _bolt.write_default_workload(td)
        )
        if not workload.is_file():
            _log.warn(
                f"[BOLT] training_workload {workload} not found — Pass 5 skipped"
            )
            return

        _log.ui(
            "[BOLT] Pass 5 — profiling clang on a compile job and rewriting "
            "with llvm-bolt (PGO→BOLT)"
        )
        try:
            fdata = _bolt.collect_profile(
                clang, store,
                _bolt.compile_workload_argv(str(clangxx), workload, td / "w.o"),
            )
            bolted = _bolt.bolt_binary(clang, fdata, out=td / "clang.bolt")
        except _bolt.BoltError as e:
            _log.warn(f"[BOLT] Pass 5 failed ({e}) — PGO clang left in place")
            return

        # Smoke-test the BOLTed clang *before* it replaces the system compiler.
        smoke = _sp.run(
            [str(bolted), "-std=c++17", "-O2", "-c", str(workload), "-o", str(td / "s.o")],
            capture_output=True, text=True,
        )
        if smoke.returncode != 0 or not (td / "s.o").exists():
            _log.warn(
                "[BOLT] the BOLT-optimized clang failed its smoke test — "
                "discarding it; the verified PGO clang stays in place"
            )
            return

        try:
            _fsp._run_priv(["install", "-Dm755", str(bolted), str(clang)])
        except _fsp.FsProvisionError as e:
            _log.warn(
                f"[BOLT] could not install the BOLTed clang ({e}) — "
                "PGO clang left in place"
            )
            return

        # Provenance sidecar (the build_state entry stays the PGO record; this
        # marks that a post-link BOLT pass was applied on top).
        with contextlib.suppress(OSError):
            (store / "applied.txt").write_text(
                f"bolt_llvm applied to {clang}\n", encoding="utf-8"
            )
        _log.ui(
            "[BOLT] Pass 5 complete — /usr/bin/clang is now PGO+BOLT optimized "
            f"(build_mode {_bolt.BUILD_MODE})"
        )
