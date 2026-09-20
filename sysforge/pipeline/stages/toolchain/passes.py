# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/passes.py — building one package, and one pass over many.

``build_pkg`` wraps a single makepkg invocation with the toolchain stage's own
concerns (compiler override, staging env, AlreadyBuilt handling); ``build_pass``
runs it across an ordered package list and is what each of the four PGO passes
actually calls.

The two live together because ``build_pass`` is only meaningful as a loop over
``build_pkg``, and separating them would put a module boundary in the middle of
one operation.
"""
from pathlib import Path

from sysforge.build_core import make_build_options
from sysforge.primitives import build_fingerprint
from sysforge.primitives.already_built import resolve_already_built
from sysforge.primitives.makepkg_flags import SYNC_FLAGS
from sysforge.primitives.makepkg_invoke import AlreadyBuilt
from sysforge.primitives.makepkg_wrapper import run as makepkg_run
from sysforge.ui import progress

from sysforge.pipeline.stages.toolchain import constants, reuse

from sysforge import log

_log = log.get_logger("TOOLCHAIN")


def build_pkg(
    name: str,
    pkgbuild_path: Path,
    options,
    cc: str | None = None,
    cxx: str | None = None,
    extra_flags: list | None = None,
    init_session: bool = False,
    compiler_flags_extra: str | None = None,
    linker_flags_extra: str | None = None,
    pgo_build: bool = False,
    pgo_env: dict | None = None,
    strip_flags: frozenset | set | None = None,
    toolchain_variant: str | None = None,
    owner_stage: str | None = None,
    cmake_llvm_dir: str | None = None,
    pgo_reuse: bool = True,
) -> None:
    """Build one package via makepkg_wrapper.run().

    ``toolchain_variant`` is stamped into build_state so ``sysforge update``
    can flag drift. Set only on install-bearing passes — intermediate PGO
    passes (1/2/3) leave it ``None`` so their (transient, soon-overwritten)
    build_state writes don't carry a misleading variant claim.

    ``owner_stage`` is the stage-ownership marker (``"toolchain"``) that makes
    ``sysforge update`` skip these LLVM packages by default and point the user
    at ``sysforge run toolchain`` instead. Like ``toolchain_variant`` it is set
    only on install-bearing passes; intermediate passes leave it ``None``.
    """
    if options.dry_run:
        cc_label = f" CC={cc}" if cc else ""
        _log.ui(f"[dry-run] would build {name}{cc_label}")
        return
    # Strip install flags — toolchain controls install/no-install via extra_flags.
    user_flags = [
        f for f in getattr(options, "makepkg_flags", []) if f not in ("-i", "--install")
    ]
    if pgo_build:
        dropped = [f for f in user_flags if f not in constants.PGO_ALLOWED_MAKEPKG_FLAGS]
        user_flags = [f for f in user_flags if f in constants.PGO_ALLOWED_MAKEPKG_FLAGS]
        if dropped:
            _log.warn(
                f"PGO build: ignoring -m flags that could corrupt the "
                f"instrumentation sequence: {dropped}",
            )
    combined_flags = list(extra_flags or []) + user_flags
    try:
        makepkg_run(pkgbuild_path, options=make_build_options(
            "toolchain", options,
            extra_flags=combined_flags,
            compiler_flags_extra=compiler_flags_extra,
            linker_flags_extra=linker_flags_extra,
            cc_override=cc,
            cxx_override=cxx,
            init_session=init_session,
            update=not options.no_update,
            strip_full_lto=pgo_build,
            extra_env=pgo_env,
            strip_flags=strip_flags,
            toolchain_variant=toolchain_variant,
            owner_stage=owner_stage,
            cmake_llvm_dir=cmake_llvm_dir,
            pgo_reuse=pgo_reuse,
        ))
    except AlreadyBuilt:
        # 2.5.1-F2: previously uncaught — a stale same-version artifact in
        # PKGDEST crashed the pass. Passes stage/install from PKGDEST, so the
        # existing artifact is the product; route the decision and continue.
        resolve_already_built("reuse", interactive=False, tag="TOOLCHAIN")
        _log.info(
            f"{name}: package already built — reusing existing artifact "
            "in PKGDEST"
        )


def build_pass(
    label: str,
    pkgbuild_map: dict[str, Path],
    options,
    cc: str | None = None,
    cxx: str | None = None,
    install: bool = True,
    compiler_flags_extra: str | None = None,
    linker_flags_extra: str | None = None,
    pgo_build: bool = False,
    pgo_env: dict | None = None,
    staged_deps: bool = False,
    toolchain_variant: str | None = None,
    owner_stage: str | None = None,
    cmake_llvm_dir: str | None = None,
    reuse_ctx: "reuse.ReuseCtx | None" = None,
    pgo_reuse: bool = True,
) -> dict[str, str]:
    """Build all packages in pkgbuild_map for one pass.

    Deduplicates by PKGBUILD directory: split packages that share a directory
    (e.g. llvm, llvm-libs, clang from the same PKGBUILD) are only built once.

    ``staged_deps=True`` means PKGBUILD-declared deps (notably ``llvm=<ver>``)
    are satisfied by a stage prefix (e.g. ``/var/tmp/sysforge-llvm-stage1``)
    rather than by installed pacman packages.  In that mode ``--syncdeps``/``-s``
    is stripped from the resolved profile's makepkg_flags and ``--nodeps`` is
    appended — otherwise makepkg would invoke ``sudo pacman -S llvm=<ver>``,
    fail with "target not found" (the version isn't published anywhere), and
    abort the pass.  Pass 1 sets staged_deps=False because it builds against
    the live system; Pass 2/3/4 set staged_deps=True.

    ``reuse_ctx`` enables opt-in input-fingerprint reuse (Pass 4 only). When set,
    each built PKGBUILD's fingerprint is computed and recorded; if
    ``reuse_ctx.consult`` and a matching, still-present artifact is cached, the build
    is *skipped* (the on-disk artifact is reused by the later staging/install).
    Returns ``{pkgbase: fingerprint}`` for the dirs built or skipped this pass —
    the caller chains it into the next sub-pass's ``staged_dep_fps`` (Merkle).

    ``pgo_reuse=False`` stops each package re-applying its own durable
    ``--pgo=use`` profile (the training-corpus pass: never ``-fprofile-use``).
    """
    extra = ["--install"] if install else []
    if pgo_build:
        extra = ["--cleanbuild", "--force"] + extra
    strip_flags: frozenset | None = None
    if staged_deps:
        extra = extra + ["--nodeps"]
        strip_flags = SYNC_FLAGS
    _log.ui(f"─── {label} ──────────────────────────────────────────")
    total = len({p.parent for p in pkgbuild_map.values()})
    seen_dirs: set[Path] = set()
    first = True
    fingerprints: dict[str, str] = {}
    with progress.tracker(total, label) as tick:
        for name, pkgbuild_path in pkgbuild_map.items():
            pkg_dir = pkgbuild_path.parent
            if pkg_dir in seen_dirs:
                _log.info(f"  {name} (split — built with {pkg_dir.name})")
                continue
            seen_dirs.add(pkg_dir)
            tick(name)

            # Input-fingerprint reuse (Pass 4, opt-in). Compute always (so a
            # first run populates the cache); skip the build only when opted in
            # AND the cached artifact set is still valid. Never active in
            # dry-run (no artifacts to validate or reuse).
            fp: str | None = None
            pkgbase = name
            if reuse_ctx is not None and not options.dry_run:
                pkgbase, fp = reuse.pkg_fingerprint(
                    reuse_ctx, name, pkgbuild_path, cc, compiler_flags_extra,
                    linker_flags_extra, cmake_llvm_dir, extra,
                )
                fingerprints[pkgbase] = fp
                if reuse_ctx.consult:
                    key = build_fingerprint.cache_key(reuse_ctx.pass_id, pkgbase)
                    hit = build_fingerprint.cache_hit(reuse_ctx.cache, key, fp)
                    if hit:
                        _log.ui(
                            f"  [PGO] reusing cached build of {pkgbase} "
                            f"({reuse_ctx.pass_id}) — fingerprint match, "
                            f"{len(hit)} artifact(s) on disk; skipping rebuild",
                        )
                        continue  # build skipped; do not consume `first`

            build_pkg(
                name,
                pkgbuild_path,
                options,
                cc=cc,
                cxx=cxx,
                extra_flags=extra,
                init_session=first,
                compiler_flags_extra=compiler_flags_extra,
                linker_flags_extra=linker_flags_extra,
                pgo_build=pgo_build,
                pgo_env=pgo_env,
                strip_flags=strip_flags,
                toolchain_variant=toolchain_variant,
                owner_stage=owner_stage,
                cmake_llvm_dir=cmake_llvm_dir,
                pgo_reuse=pgo_reuse,
            )
            first = False

            if reuse_ctx is not None and fp is not None:
                members = [n for n, p in pkgbuild_map.items() if p.parent == pkg_dir]
                search_dirs = ([reuse_ctx.pkgdest] if reuse_ctx.pkgdest else []) + [pkg_dir]
                key = build_fingerprint.cache_key(reuse_ctx.pass_id, pkgbase)
                build_fingerprint.record_build(
                    reuse_ctx.cache, key, fp, search_dirs, members,
                )
                build_fingerprint.save_cache(reuse_ctx.cache_path, reuse_ctx.cache)
    return fingerprints
