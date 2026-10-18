# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/bolt.py — BOLT the PGO toolchain inside the llvm build (3.5.0-F3).

Runs right after Pass 4a, between the build and the install. Upstream builds
clang and BOLT in the same tree as llvm, so after 4a the build directory holds
unstripped, relocation-carrying ``libclang-cpp.so``/``libLLVM.so`` and the
in-tree ``llvm-bolt``. This step instruments those libraries, trains them by
replaying a sample of the tree's own compile commands with the new clang,
rewrites them with the merged profile, smoke-tests the result, swaps it into
the tree keeping the original mtime, and has makepkg repackage. The facts and
why each is needed are in ``primitives/bolt.py``.

Strictly optional: any failure before the repackage restores the tree and
leaves the 4a packages exactly as built, so the PGO toolchain still installs. A
failed repackage stops the stage before install, since the 4a packages may be
half-rewritten by then.

Separate from ``pgo.py`` because it is a different tool with a different
failure mode — the four PGO passes are one interdependent sequence, while this
either improves the result or is skipped.
"""
import contextlib
import os
import shutil
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sysforge.pipeline.stages.toolchain import config

from sysforge import log
from sysforge.primitives import bolt as _bolt
from sysforge.primitives import fs_provision
from sysforge.primitives import run

_log = log.get_logger("TOOLCHAIN")

# Compile commands replayed to train the instrumented libraries. Enough of
# clang's own front-end sources to cover parsing, Sema and codegen; the cost is
# a few minutes of instrumented compiles on top of a multi-hour build.
TRAINING_UNITS = 400

# Fewer successful training compiles than this share of the attempted ones
# means the instrumented libraries are not working: do not trust the profile.
_MIN_TRAINING_SHARE = 0.5


def _store(tcfg: "config.ToolchainConfig") -> Path:
    return _bolt.resolve_store(tcfg.raw)


def _run_training(cmds: list[list[str]], *, env: dict, cwd: Path) -> int:
    """Run the training compiles in parallel; return how many succeeded."""
    def one(argv: list[str]) -> bool:
        return run.probe(argv, env=env, cwd=cwd).returncode == 0

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 1) as pool:
        return sum(pool.map(one, cmds))


def _mirror_symlinks(lib_dir: Path, target: Path, into: Path) -> None:
    """Give ``into`` the soname symlinks ``lib_dir`` has for ``target``, so the
    dynamic loader finds the rewritten copy under every name it is asked for."""
    for link in lib_dir.iterdir():
        if link.is_symlink() and link.readlink() == Path(target.name):
            with contextlib.suppress(FileExistsError):
                (into / link.name).symlink_to(target.name)


def _cycle(build: Path, targets: list[Path], workload: str, td: Path) -> dict[Path, Path]:
    """Instrument → train → merge → optimize → smoke-test. Returns
    ``{build-tree lib: rewritten copy}``; raises :class:`BoltError`."""
    llvm_bolt = _bolt.tree_tool(build, "llvm-bolt")
    merge_fdata = _bolt.tree_tool(build, "merge-fdata")
    runtime = _bolt.tree_tool(build, "libbolt_rt_instr.a")
    clangxx = _bolt.tree_tool(build, "clang++")
    missing = [p.name for p in (llvm_bolt, merge_fdata, runtime, clangxx) if not p.exists()]
    if missing:
        raise _bolt.BoltError(
            f"the build tree has no {', '.join(missing)}: upstream's llvm PKGBUILD "
            "builds bolt in-tree since 23.1.3-3; an older pin does not")
    if not _bolt.runtime_linkable(runtime):
        raise _bolt.BoltError(
            f"{runtime} calls memset through the GOT (built by GCC with -fno-plt), "
            "which llvm-bolt's linker rejects for every input")
    for lib in targets:
        if not _bolt.has_relocations(lib):
            raise _bolt.BoltError(
                f"{lib.name} has no relocations: it was not linked with "
                f"{_bolt.EMIT_RELOCS_LDFLAG}")

    inst, prof, opt, obj = (td / d for d in ("inst", "prof", "opt", "obj"))
    for d in (inst, prof, opt, obj):
        d.mkdir()
    for lib in targets:
        _bolt.run_step(
            _bolt.instrument_cmd(llvm_bolt, lib, inst / lib.name,
                                 prof / f"{lib.name}.fdata", runtime),
            what=f"instrument {lib.name}")
        _mirror_symlinks(lib.parent, lib, inst)

    if workload:
        src = Path(workload).expanduser()
        if not src.is_file():
            raise _bolt.BoltError(f"[bolt] training_workload {src} not found")
        cmds = [_bolt.compile_workload_argv(clangxx, src, obj / "w.o")]
    else:
        cmds = _bolt.training_commands(
            build, clangxx, obj, target=f"lib/{targets[0].name}", limit=TRAINING_UNITS)
        if not cmds:
            src = _bolt.write_default_workload(td)
            cmds = [_bolt.compile_workload_argv(clangxx, src, obj / "w.o")]
    lib_path = f"{inst}:{build / 'lib'}"
    _log.ui(f"[BOLT] training: {len(cmds)} compile(s) with the instrumented libraries")
    ok = _run_training(cmds, env={**os.environ, "LD_LIBRARY_PATH": lib_path}, cwd=build)
    if ok < max(1, int(len(cmds) * _MIN_TRAINING_SHARE)):
        raise _bolt.BoltError(
            f"only {ok} of {len(cmds)} training compiles succeeded with the "
            "instrumented libraries")

    rewritten: dict[Path, Path] = {}
    for lib in targets:
        profiles = sorted(prof.glob(f"{lib.name}.fdata.*.fdata"))
        if not profiles:
            raise _bolt.BoltError(f"training wrote no profile for {lib.name}")
        merged = td / f"{lib.name}.merged.fdata"
        _bolt.run_step(_bolt.merge_cmd(merge_fdata, profiles, merged),
                       what=f"merge {len(profiles)} profile(s) for {lib.name}")
        _bolt.run_step(_bolt.optimize_cmd(llvm_bolt, lib, merged, opt / lib.name),
                       what=f"optimize {lib.name}")
        _mirror_symlinks(lib.parent, lib, opt)
        rewritten[lib] = opt / lib.name

    _bolt.run_step(
        cmds[0], what="smoke test (compile with the rewritten libraries)",
        env={**os.environ, "LD_LIBRARY_PATH": f"{opt}:{build / 'lib'}"}, cwd=build)
    return rewritten


def _keep_profiles(tcfg: "config.ToolchainConfig", td: Path, targets: list[Path]) -> None:
    """Copy the merged profiles into the BOLT store, for the record."""
    store = _store(tcfg)
    try:
        fs_provision.ensure_writable_dir(store)
        for lib in targets:
            merged = td / f"{lib.name}.merged.fdata"
            if merged.is_file():
                shutil.copy2(merged, store / f"{lib.name}.fdata")
    except (OSError, fs_provision.FsProvisionError) as e:
        _log.warn(f"[BOLT] could not keep the profiles in {store}: {e}")


def optimize_in_build(
    pgo_map: dict, tcfg: "config.ToolchainConfig", options, *,
    repackage: Callable[[], None],
) -> bool:
    """BOLT the Pass-4a build tree and repackage; True when the packages changed.

    No-op unless ``[bolt] enabled`` (and not a dry run). ``repackage`` reruns
    makepkg's ``package()`` for the llvm PKGBUILD (``--repackage``) with the
    same settings 4a used; the caller owns those.
    """
    bcfg = tcfg.bolt
    if not bcfg.enabled:
        return False
    if options.dry_run:
        _log.ui("[dry-run] would BOLT libclang-cpp"
                f"{' and libLLVM' if bcfg.libllvm else ''} in the llvm build tree")
        return False
    pkgbuild = pgo_map.get("clang")
    if pkgbuild is None:
        _log.warn(
            "[BOLT] skipped: clang is not built by the PGO PKGBUILD, so its "
            "libraries are not in the Pass-4a build tree (BOLT needs upstream's "
            "single llvm package). The PGO toolchain is unaffected.")
        return False
    build = _bolt.build_tree_for(Path(pkgbuild))
    if build is None:
        _log.warn(f"[BOLT] skipped: no llvm build tree found for {pkgbuild}")
        return False
    targets = _bolt.bolt_targets(build, libllvm=bcfg.libllvm)
    if not targets:
        _log.warn(f"[BOLT] skipped: no libclang-cpp in {build / 'lib'}")
        return False

    names = ", ".join(t.name for t in targets)
    _log.ui(f"[BOLT] optimizing {names} in {build}")
    with tempfile.TemporaryDirectory(prefix="sysforge-bolt-") as tmp:
        td = Path(tmp)
        try:
            rewritten = _cycle(build, targets, bcfg.training_workload, td)
        except _bolt.BoltError as e:
            _log.warn(f"[BOLT] skipped: {e}. The PGO toolchain installs un-BOLTed.")
            return False

        backup = td / "orig"
        backup.mkdir()
        for lib in targets:
            shutil.copy2(lib, backup / lib.name)
        for lib, new in rewritten.items():
            _bolt.replace_keep_mtime(new, lib)
        try:
            repackage()
        except Exception as e:
            for lib in targets:
                _bolt.replace_keep_mtime(backup / lib.name, lib)
            raise RuntimeError(
                f"[BOLT] repackaging llvm after BOLT failed ({e}); the Pass-4a "
                "packages may be partly rewritten, so nothing was installed. "
                "Rerun with [bolt] enabled = false to install the PGO toolchain "
                "without BOLT.") from e
        _keep_profiles(tcfg, td, targets)
    _log.ui(f"[BOLT] {names} rewritten and repackaged (build_mode {_bolt.BUILD_MODE})")
    return True
