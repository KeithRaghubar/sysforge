# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/verify.py — did the toolchain we just installed actually work?

Post-install verification for the LLVM path. The failure this exists to catch
is specific and nasty: a suite that installs cleanly but whose clang cannot
resolve libLLVM symbols at runtime, because two halves were linked against
different libLLVM versions. That is invisible to makepkg and to pacman, and it
bricks every subsequent build on the machine.

So verification is evidence-based rather than exit-code-based — it reads
dynamic symbol tables directly (``nm``) and compares what a binary *needs*
against what the installed libLLVM *provides*.
"""
from pathlib import Path
from sysforge.primitives import run

from sysforge.primitives import toolchain_safety
from sysforge.primitives.toolchain_preflight import LLVM_LOCKSTEP_SUITE

from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Post-install verification (LLVM only)
# ---------------------------------------------------------------------------

# Packages whose installed version must match across the LLVM toolchain set.
# A mismatch — typically from an interrupted `pacman -U` of one pass — is
# what produces the broken-GUI / missing-symbol failure mode this guard is
# here to catch. Shares the single source of truth with the preflight skew
# probe (LLVM_LOCKSTEP_SUITE) so the two checks never diverge.
_LLVM_VERSION_MATCH_SET = LLVM_LOCKSTEP_SUITE


def query_pacman_versions(pkgnames: tuple[str, ...]) -> dict[str, str | None]:
    """Return {pkgname: version_string-or-None} via a single ``pacman -Q``.

    A missing package maps to None. Used by :func:`verify_llvm_install`
    to assert that every installed LLVM component reports the same
    `pkgver-pkgrel`.
    """
    result: dict[str, str | None] = {n: None for n in pkgnames}
    proc = run.capture(["pacman", "-Q", *pkgnames])
    if proc is None:
        return result
    for line in proc.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2 and parts[0] in result:
            result[parts[0]] = parts[1].strip()
    return result


def check_llvm_link_resolution() -> list[str]:
    """Verify installed LLVM binaries resolve libLLVM only under /usr/lib.

    A stage prefix (``/var/tmp/sysforge-llvm-stage*``) appearing in the
    ``ldd`` output of an installed binary means Pass 4 packaged a bad
    RPATH or the install is incomplete — silently leaving the system on a
    libLLVM that's about to be wiped from /var/tmp.  A resolution under
    some other prefix (``$HOME/.local/lib`` etc.) is also flagged because
    it means a sibling library is shadowing the package-managed one.

    Returns issue strings; empty list means clean.
    """
    issues: list[str] = []
    for bin_path, label in (
        ("/usr/bin/clang", "clang"),
        ("/usr/bin/lld", "lld"),
    ):
        if not Path(bin_path).exists():
            continue
        proc = run.capture(["ldd", bin_path])
        if proc is None:
            issues.append("ldd: binary missing from PATH")
            return issues
        if proc.returncode != 0:
            issues.append(f"ldd {bin_path}: exit {proc.returncode}")
            continue
        libllvm_paths: list[str] = []
        for line in proc.stdout.splitlines():
            stripped = line.strip()
            if "libLLVM" not in stripped:
                continue
            parts = stripped.split("=>")
            if len(parts) < 2:
                continue
            tail = parts[1].strip().split()
            if not tail or tail[0] in ("not", "(0x0)"):
                continue
            libllvm_paths.append(tail[0])
        for p in libllvm_paths:
            if "/var/tmp/sysforge-llvm-stage" in p:  # noqa: S108 — reference literal for a diagnostic substring check, not a temp file created here
                issues.append(
                    f"{label} resolves libLLVM from a staging prefix: {p} "
                    "— Pass 4 packaged a bad RPATH or the install is incomplete"
                )
            elif not p.startswith("/usr/lib"):
                issues.append(
                    f"{label} resolves libLLVM outside /usr/lib: {p} "
                    "— a sibling libLLVM is shadowing the package-managed one"
                )
    return issues


def _nm_dynsyms(so: Path, *, undefined: bool) -> list[str]:
    """Return ``nm -D`` symbol names (with their ``@version`` suffix) from ``so``.

    ``undefined=True`` lists undefined references (``U``); otherwise defined
    exports (``--defined-only``). Returns ``[]`` if ``nm`` is missing or fails.
    """
    flag = "--undefined-only" if undefined else "--defined-only"
    proc = run.capture(["nm", "-D", flag, str(so)])
    if proc is None:
        return []
    if proc.returncode != 0:
        return []
    return [parts[-1] for parts in (ln.split() for ln in proc.stdout.splitlines()) if parts]


def _so_ver(path: Path) -> tuple[int, ...]:
    """Numeric version tuple parsed from a ``lib*.so.<ver>`` filename (() if none).

    ``libLLVM.so.22.1`` → ``(22, 1)``; stops at the first non-numeric component
    so a plain ``lib*.so`` symlink yields ``()``. Used to compare sonames
    numerically rather than lexically (``.21.1`` must not sort ahead of ``.9``).
    """
    marker = ".so."
    idx = path.name.rfind(marker)
    if idx == -1:
        return ()
    out: list[int] = []
    for part in path.name[idx + len(marker):].split("."):
        if not part.isdigit():
            break
        out.append(int(part))
    return tuple(out)


def _newest_so(base: Path, name_glob: str) -> Path | None:
    """Highest-versioned regular ``base/usr/lib/<name_glob>`` match.

    Unlike :func:`_first_so` (lexical first), this picks the newest soname, so a
    compat package's older library (e.g. ``llvm21-libs``'s ``libLLVM.so.21.1``
    sitting alongside ``llvm-libs``'s ``libLLVM.so.22.1``) never shadows the
    real, just-installed one.
    """
    cands = [p for p in (base / "usr/lib").glob(name_glob)
             if p.is_file() and not p.is_symlink()]
    if not cands:
        return None
    return max(cands, key=_so_ver)


def dump_stage_dynsym_evidence(
    staging3: Path,
    dest_dir: Path | str | None,
    *,
    install_root: Path = Path("/"),
) -> Path | None:
    """Capture Gate-3 symbol-version brick evidence to ``llvm_abi_hazard.log``.

    The brick is a C++ stdlib symbol bound to libLLVM's ``LLVM_<ver>`` version
    node: a consumer (``libclang-cpp`` / ``liblldCommon``) carries an *undefined*
    ``_ZNSt*@LLVM_<ver>`` reference that the *installed* ``libLLVM`` does not
    export. The actionable evidence is the **difference** between what the
    installed consumers demand under ``@LLVM_*`` and what the installed
    ``libLLVM`` provides — read straight from the live ``/usr`` files that
    bricked, not from a staging prefix (the previous stage2 dump captured an
    unrelated, often stale, library and never contained the brick symbols).

    ``staging3`` (the libLLVM Pass 4b linked against) is included for contrast:
    if it exports the missing symbols but the installed ``libLLVM`` does not, the
    split staged the wrong/incomplete libLLVM (e.g. a missing ``LLVMConfig.cmake``
    sent ``find_package(LLVM)`` back to ``/usr``). Returns the log path, or
    ``None`` when the dest is unset or no ``libLLVM`` is installed.
    """
    if dest_dir is None:
        return None

    # Consumers that bind C++ stdlib into the LLVM version namespace and brick.
    consumers = [
        c for c in (
            _newest_so(install_root, "libclang-cpp.so.*"),
            _newest_so(install_root, "liblldCommon.so.*"),
        )
        if c is not None
    ]
    # Select the installed libLLVM whose soname matches what the (newest)
    # consumers link — NOT the lexical-first glob, which picks a compat package's
    # older libLLVM.so.<old> (e.g. llvm21-libs' .21.1) ahead of the just-built
    # .22.1 and reports a false "0 NOT provided" all-clear. clang/llvm are in
    # version lockstep, so the consumer soname version == the wanted libLLVM one.
    target_ver = max((_so_ver(c) for c in consumers), default=())
    installed_libllvm: Path | None = None
    if target_ver:
        cand = (install_root / "usr/lib"
                / f"libLLVM.so.{'.'.join(str(x) for x in target_ver)}")
        if cand.is_file():
            installed_libllvm = cand
    if installed_libllvm is None:
        installed_libllvm = _newest_so(install_root, "libLLVM.so.*")
    if installed_libllvm is None:
        return None

    def _llvm_std(syms: list[str]) -> set[str]:
        # Normalise @@ (defined) and @ (undefined) so the two sets compare;
        # keep only C++ stdlib symbols bound to an LLVM version node.
        return {
            s.replace("@@", "@")
            for s in syms
            if "_ZNSt" in s and "@LLVM_" in s
        }

    provided = _llvm_std(_nm_dynsyms(installed_libllvm, undefined=False))
    staged_libllvm = _newest_so(staging3, "libLLVM.so.*")
    staged_provided = (
        _llvm_std(_nm_dynsyms(staged_libllvm, undefined=False))
        if staged_libllvm is not None
        else set()
    )

    lines: list[str] = [
        "# Gate-3 ABI brick evidence — C++ stdlib symbols bound to LLVM_<ver>",
        f"# installed libLLVM: {installed_libllvm}  "
        f"({len(provided)} _ZNSt*@LLVM_* exports)",
        f"# staged   libLLVM: {staged_libllvm or '(absent)'}  "
        f"({len(staged_provided)} _ZNSt*@LLVM_* exports)",
        "",
    ]
    for so in consumers:
        demanded = _llvm_std(_nm_dynsyms(so, undefined=True))
        missing = sorted(demanded - provided)
        lines.append(
            f"## {so}: {len(demanded)} _ZNSt*@LLVM_* undefined refs, "
            f"{len(missing)} NOT provided by installed libLLVM"
        )
        if missing:
            lines.append(
                "# >>> brick cause: demanded under @LLVM_* but absent in installed libLLVM:"
            )
            lines.extend(missing)
            in_staged = [m for m in missing if m in staged_provided]
            if in_staged:
                lines.append(
                    f"# note: {len(in_staged)}/{len(missing)} of these ARE exported by "
                    "the staged (staging3) libLLVM — the shipped libLLVM differs "
                    "from what Pass 4b linked against:"
                )
                lines.extend(in_staged)
        lines.append("")

    lines.append(
        f"# Full nm -D --defined-only {installed_libllvm} (_ZNSt* only):"
    )
    lines.extend(
        sorted(s for s in _nm_dynsyms(installed_libllvm, undefined=False) if "_ZNSt" in s)
    )

    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / "llvm_abi_hazard.log"
    out.write_text("\n".join(lines) + "\n")
    return out


def verify_llvm_install(
    expected_targets: list[str] | None = None,
) -> list[str]:
    """Run post-install LLVM consistency checks.

    Returns a list of human-readable issue strings; empty list means
    everything is consistent. Caller decides what to do (typically: prompt
    the user with a recovery `pacman -S ...` command). Checks:

    1. ``pacman -Q`` versions across :data:`_LLVM_VERSION_MATCH_SET` all
       match. A mismatch is the canonical interrupted-install symptom.
    2. ``clang --version`` and ``ld.lld --version`` run without crashing.
    3. ``llvm-config --targets-built`` is a superset of
       ``expected_targets`` (skipped when ``expected_targets`` is None or
       empty — i.e. no LLVM_TARGETS filtering was configured).
    4. ``ldd`` of installed clang/lld resolves libLLVM under /usr/lib —
       never under /var/tmp/sysforge-llvm-stage*.  Catches a Pass-4 RPATH
       mistake before /var/tmp gets cleaned and breaks the live toolchain.
    """
    issues: list[str] = []

    # Skew arm — drawn from the pure fact in toolchain_safety so this entry
    # point and the preflight probe share one definition (the data source,
    # query_pacman_versions, stays here so the subprocess call remains the
    # patch point). detect_suite_skew is brick-class on disagreement.
    versions = query_pacman_versions(_LLVM_VERSION_MATCH_SET)
    skew = toolchain_safety.detect_suite_skew(versions)
    if skew is not None:
        issues.append(skew.message)

    issues.extend(check_llvm_link_resolution())

    # Probe ``ld.lld``, not bare ``lld``: ``lld`` is the generic multiplexer
    # driver and dispatches on argv[0], so ``lld --version`` always exits 1
    # ("lld is a generic driver"). ``ld.lld`` is the GNU-compatible flavor that
    # ``-fuse-ld=lld`` actually resolves to for every build pass.
    for cmd, label in (
        (["clang", "--version"], "clang --version"),
        (["ld.lld", "--version"], "ld.lld --version"),
    ):
        proc = run.capture(cmd)
        if proc is None:
            issues.append(f"{label}: binary missing from PATH")
            continue
        if proc.returncode != 0:
            issues.append(
                f"{label}: exit {proc.returncode} — {proc.stderr.strip()[:200]}"
            )

    if expected_targets:
        proc = run.capture(["llvm-config", "--targets-built"])
        if proc is None:
            issues.append("llvm-config: binary missing from PATH")
        else:
            if proc.returncode != 0:
                issues.append(
                    f"llvm-config --targets-built: exit {proc.returncode}"
                )
            else:
                built = set(proc.stdout.split())
                missing = [t for t in expected_targets if t not in built]
                if missing:
                    issues.append(
                        "llvm-config --targets-built missing expected backends "
                        f"({', '.join(missing)}); built={sorted(built)}"
                    )

    return issues


def llvm_recovery_command() -> str:
    """The canonical pacman command that restores a consistent LLVM set."""
    return "sudo pacman -S " + " ".join(_LLVM_VERSION_MATCH_SET)
