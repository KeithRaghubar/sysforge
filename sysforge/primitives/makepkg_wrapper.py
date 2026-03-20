"""
makepkg_wrapper.py — build execution

Responsible for conf file emission, makepkg invocation, retry logic,
and the top-level run() entry point. All config loading, profile resolution,
and PKGBUILD parsing are delegated to their respective modules.

Public API:
    run(pkgbuild_path)
"""
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from sysforge.primitives.resource_guard import lift_for_child

from sysforge.primitives.config import (
    find_pkgbuild,
    load_config,
    load_conflict_groups,
    load_consumes_inference,
    parse_system_makepkg_conf,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import (
    apply_patch_pkgbuild,
    cleanup_patch_artifacts,
    extract_pkgbuild_profile,
    patch_noninteractive_kconfig,
    patch_pkgbuild_groups,
    write_extracted_profile,
)
from sysforge.primitives.cache_probe import (
    diff_ccache,
    diff_sccache,
    emit_build_stats,
    emit_session_report,
    emit_system_probes,
    probe_ccache,
    probe_sccache,
    probe_thinlto_cache,
    record_build_result,
    reset_session,
)
from sysforge.primitives.aur import import_pgp_keys, git_pull_rebase
from sysforge.primitives.dep_analysis import run_dep_analysis
from sysforge.primitives.failure import handle_failure
import sysforge.log as _log
from sysforge.primitives.profile import (
    _CONF_KEY_MAP,
    _KERNEL_CLEAN_KEYS,
    _SYSFORGE_KEYS,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
    serialize_flags,
)


# ---------------------------------------------------------------------------
# Conf file emission
# ---------------------------------------------------------------------------

# Flags that are lld-specific and must be stripped when lld is not the active linker.
# These may appear as bare tokens in LDFLAGS or as sub-tokens inside -Wl,... sequences.
_LLD_ONLY_FLAGS = frozenset({
    "--icf=all",
    "--icf=safe",
    "--icf=none",
})

# Full LTO flags incompatible with clang IR PGO. ThinLTO (-flto=thin) is
# compatible and must NOT be stripped. Bare -flto defaults to full in clang.
_FULL_LTO_FLAGS = frozenset({"-flto", "-flto=full", "-flto=auto"})


def _detect_linker_from_ldflags(ldflags_val):
    """Return the linker name declared by -fuse-ld=X in LDFLAGS, or None."""
    for token in ldflags_val.split():
        if token.startswith("-fuse-ld="):
            return token[len("-fuse-ld="):]
    return None


def _inject_linker(ldflags_val, linker_name):
    """
    Replace an existing -fuse-ld=X token in LDFLAGS, or prepend one if absent.
    Returns the updated LDFLAGS string.
    """
    new_token = f"-fuse-ld={linker_name}"
    tokens = ldflags_val.split() if ldflags_val else []
    for i, t in enumerate(tokens):
        if t.startswith("-fuse-ld="):
            if tokens[i] != new_token:
                _log.info("[FLAG]", f"Replaced {tokens[i]} with {new_token} (--ld override)")
            tokens[i] = new_token
            return " ".join(tokens)
    _log.info("[FLAG]", f"Injected {new_token} into LDFLAGS (--ld override)")
    return " ".join([new_token] + tokens)


def _strip_full_lto(flags_val: str) -> tuple[str, list[str]]:
    """
    Remove full-LTO flags from a compiler flag string.
    ThinLTO (-flto=thin) is preserved — it is compatible with clang IR PGO.
    Returns (cleaned_str, list_of_stripped_tokens).
    """
    stripped = []
    kept = []
    for token in flags_val.split():
        if token in _FULL_LTO_FLAGS:
            stripped.append(token)
        else:
            kept.append(token)
    return " ".join(kept), stripped


def _strip_lld_flags(ldflags_val):
    """
    Remove lld-specific flags from an LDFLAGS string.
    Handles bare tokens (--icf=all) and flags embedded in -Wl,... sequences.
    Returns (cleaned_str, list_of_stripped_tokens).
    """
    stripped = []
    out_tokens = []
    for token in ldflags_val.split():
        if token.startswith("-Wl,"):
            subtokens = token[4:].split(",")
            kept = []
            for sub in subtokens:
                if sub in _LLD_ONLY_FLAGS:
                    stripped.append(sub)
                else:
                    kept.append(sub)
            if kept:
                out_tokens.append("-Wl," + ",".join(kept))
        elif token in _LLD_ONLY_FLAGS:
            stripped.append(token)
        else:
            out_tokens.append(token)
    return " ".join(out_tokens), stripped


@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile, active_consumes=None,
                      system_conf_path=None,
                      cc_override=None, cxx_override=None, ld_override=None,
                      kernel_build: bool = False,
                      compiler_flags_extra: str | None = None,
                      strip_full_lto: bool = False):
    """
    Write a complete, self-contained temp makepkg.conf by merging:
      1. All keys from /etc/makepkg.conf (system baseline)
      2. Profile-managed keys from resolved_profile (override)
      3. CLI overrides (--cc, --cxx, --ld) applied on top of profile values

    Keys in the system conf that sysforge doesn't manage (CARCH, CHOST,
    PKGDEST, PACKAGER, etc.) are written verbatim from the system conf.
    Profile keys overwrite their system counterparts; profile keys absent
    from the system conf are appended at the end.

    "env" type keys and sysforge-internal keys are never written to the conf.

    Linker guard: determines the effective linker from LDFLAGS. If no -fuse-ld=X
    is declared, the effective linker is the system default (bfd). lld-specific
    flags are stripped whenever the effective linker is not lld.

    kernel_build: when True, _KERNEL_CLEAN_KEYS (CFLAGS, CXXFLAGS, LDFLAGS,
    CPPFLAGS, DEBUG_*) are excluded from profile overrides and ld_override is
    ignored. System conf values for those keys pass through verbatim.

    compiler_flags_extra: when set, appended verbatim to CFLAGS, CXXFLAGS, and
    LDFLAGS in the emitted conf after all other processing (including the linker
    guard). Intended for PGO generate/use flags injected by the toolchain stage.
    """
    env_keys = _CONF_KEY_MAP.get("env", set())

    if active_consumes is None:
        allowed_keys = None
    else:
        allowed_keys: set[str] = set()
        for conf_type in active_consumes:
            if conf_type == "env":
                continue
            if conf_type in _CONF_KEY_MAP:
                allowed_keys.update(_CONF_KEY_MAP[conf_type])

    # Profile keys to write: filter out internal, env-pass, out-of-consumes,
    # and (for kernel builds) compiler flag keys.
    profile_overrides = {}
    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue
        if key in env_keys:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if kernel_build and key in _KERNEL_CLEAN_KEYS:
            continue
        profile_overrides[key] = val

    if kernel_build:
        _log.info("[KERNEL]", "Kernel build: omitting profile flag keys from makepkg.conf "
                  "(CFLAGS/CXXFLAGS/LDFLAGS/CPPFLAGS/DEBUG_*); system conf values preserved")

    # Load system conf baseline
    system_assignments = parse_system_makepkg_conf(system_conf_path)

    # Apply CLI toolchain overrides on top of profile values
    if cc_override is not None:
        profile_overrides["CC"] = cc_override
        _log.info("[FLAG]", f"CC overridden via --cc: {cc_override}")
    if cxx_override is not None:
        profile_overrides["CXX"] = cxx_override
        _log.info("[FLAG]", f"CXX overridden via --cxx: {cxx_override}")
    if ld_override is not None:
        if kernel_build:
            _log.info("[KERNEL]", f"--ld={ld_override!r} ignored for kernel build (use LLVM=1 for lld)")
        else:
            current_ldflags = profile_overrides.get("LDFLAGS", "")
            profile_overrides["LDFLAGS"] = _inject_linker(current_ldflags, ld_override)

    # Linker guard: determine the effective linker. If no -fuse-ld=X is declared,
    # the effective linker is the system default (bfd). Strip lld-specific flags
    # whenever the effective linker is not lld — not only when a linker is declared
    # but missing, since undeclared LDFLAGS containing lld-only flags will break
    # configure test compilations against the system linker.
    if "LDFLAGS" in profile_overrides:
        declared_linker = _detect_linker_from_ldflags(profile_overrides["LDFLAGS"])
        effective_linker = declared_linker or "ld"

        if declared_linker and not shutil.which(declared_linker):
            _log.warn("[FLAG]", f"Declared linker '{declared_linker}' not found on PATH — treating effective linker as 'ld'")
            effective_linker = "ld"

        if effective_linker != "lld":
            cleaned, stripped_tokens = _strip_lld_flags(profile_overrides["LDFLAGS"])
            if stripped_tokens:
                _log.warn("[FLAG]", f"Effective linker is '{effective_linker}' (not lld) — stripping lld-specific flags from LDFLAGS")
                for tok in stripped_tokens:
                    _log.warn("[FLAG]", f"Stripped lld-only flag: {tok}")
                profile_overrides["LDFLAGS"] = cleaned

    # Full LTO stripping for PGO passes. -flto/-flto=full are incompatible with
    # clang IR PGO; -flto=thin is compatible and is preserved.
    if strip_full_lto:
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                cleaned, stripped_toks = _strip_full_lto(profile_overrides[key])
                if stripped_toks:
                    _log.warn("[PGO]",
                              f"Stripped full-LTO flag(s) from {key} (incompatible with IR PGO): "
                              f"{' '.join(stripped_toks)}")
                    profile_overrides[key] = cleaned

    # Per-invocation compiler flag injection (e.g. PGO generate/use flags).
    # Runs after the linker guard so these flags are never treated as lld-specific.
    if compiler_flags_extra:
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                base = profile_overrides[key]
            elif key in system_assignments:
                raw = system_assignments[key].strip()
                base = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
            else:
                base = ""
            profile_overrides[key] = (base + " " + compiler_flags_extra).strip()
        _log.info("[PGO]", f"Injecting into CFLAGS/CXXFLAGS/LDFLAGS: {compiler_flags_extra!r}")

    # Build output lines: system conf keys in their original raw form,
    # profile-overridden keys substituted inline, new profile keys appended.
    conf_lines = ["# Generated by SysForge — merged system conf + profile overrides"]

    for key, raw_val in system_assignments.items():
        if key in env_keys:
            # Env-type keys are injected via subprocess env, never written to conf —
            # even if the system conf has them set.
            continue
        if key in profile_overrides:
            conf_lines.append(f'{key}="{profile_overrides[key]}"')
        else:
            conf_lines.append(f"{key}={raw_val}")

    new_keys = [k for k in profile_overrides if k not in system_assignments]
    if new_keys:
        conf_lines.append("")
        conf_lines.append("# SysForge profile additions (not in system conf)")
        for key in new_keys:
            conf_lines.append(f'{key}="{profile_overrides[key]}"')

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="sysforge_makepkg_",
        suffix=".conf",
        delete=False,
    ) as f:
        f.write("\n".join(conf_lines) + "\n")
        tmp_path = f.name

    _log.info("[CONF]", f"Wrote temp makepkg.conf: {tmp_path}")
    _log.debug("[CONF]", f"Temp makepkg.conf contents:\n{chr(10).join(conf_lines)}")
    try:
        yield tmp_path
    finally:
        os.unlink(tmp_path)
        _log.info("[CONF]", f"Removed temp makepkg.conf: {tmp_path}")


# ---------------------------------------------------------------------------
# Env var resolution and build invocation
# ---------------------------------------------------------------------------

def resolve_env_vars(resolved_profile, active_consumes=None):
    """
    Extract profile keys that travel via subprocess env injection rather than
    the makepkg.conf temp file.

    Three categories are collected:
      1. Keys in the "toolchain" conf type (CC, CXX) — always injected,
         regardless of active_consumes. makepkg does not export CC/CXX from
         makepkg.conf to child processes; they must be in the inherited env.
      2. Keys in the "env" conf type — only collected when "env" is in
         active_consumes or active_consumes is None (fallback mode).
      3. Unknown keys — not in any _CONF_KEY_MAP type and not in _SYSFORGE_KEYS.
         Always collected and logged under [ENV] as a warning.

    Returns dict[str, str] of key -> value pairs to inject on invocation.
    Empty dict if nothing to inject.
    """
    toolchain_keys = _CONF_KEY_MAP.get("toolchain", set())
    env_type_keys  = _CONF_KEY_MAP.get("env", set())

    # All keys explicitly classified into any conf type
    all_conf_keys: set[str] = set()
    for keys in _CONF_KEY_MAP.values():
        all_conf_keys.update(keys)

    collect_env_type = active_consumes is None or "env" in active_consumes

    result: dict[str, str] = {}
    unknown: list[str] = []

    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue

        if key in toolchain_keys:
            # Always delivered via env — makepkg doesn't export CC/CXX from conf
            result[key] = val
            _log.info("[ENV]", f"Injecting (toolchain): {key}={val!r}")
            continue

        if key in env_type_keys:
            if collect_env_type:
                result[key] = val
                _log.info("[ENV]", f"Injecting (env type): {key}={val!r}")
            else:
                _log.info("[ENV]", f"Skipping env-type key {key!r} (not in active_consumes)")
            continue

        if key not in all_conf_keys:
            # Unknown key — not classified; env pass with warning
            result[key] = val
            unknown.append(key)

    if unknown:
        _log.warn("[ENV]", f"Unclassified profile keys injected via env (consider adding to _CONF_KEY_MAP): {sorted(unknown)}")

    return result


def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                   extra_env=None, extra_flags=None, interactive=False):
    pkgbuild_path = Path(pkgbuild_path).resolve()
    build_dir = pkgbuild_path.parent

    env = os.environ.copy()

    # Strip Python venv from the environment so makepkg subprocesses use the
    # system Python, not sysforge's venv. Without this, PKGBUILD build()
    # functions that invoke `python` or `python -m build` get the venv Python,
    # which lacks packages like `build` and produces misleading failures.
    venv_dir = env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    if venv_dir:
        venv_bin = os.path.join(venv_dir, "bin")
        path_parts = env.get("PATH", "").split(os.pathsep)
        env["PATH"] = os.pathsep.join(p for p in path_parts if p != venv_bin)
        _log.info("[ENV]", f"Stripped venv from PATH/VIRTUAL_ENV: {venv_dir}")

    # Strip all makepkg-managed and toolchain keys from the inherited shell env
    # so the temp conf and profile env injection are the sole authority.
    # Without this, shell vars like CC=clang or CFLAGS=... win over what the
    # conf/profile sets, producing unpredictable builds.
    _strip_keys = _CONF_KEY_MAP.get("makepkg", set()) | _CONF_KEY_MAP.get("toolchain", set())
    for k in sorted(_strip_keys):
        if k in env:
            _log.info("[ENV]", f"Stripped from shell env (superseded by profile): {k}={env.pop(k)!r}")

    env["MAKEPKG_CONF"] = str(conf_path)

    if extra_env:
        for k, v in sorted(extra_env.items()):
            if k in env:
                _log.warn("[ENV]", f"Overriding shell {k}={env[k]!r} with profile value {v!r}")
        env.update(extra_env)

    flags = list(resolved_profile.get("makepkg_flags", []))
    if interactive:
        flags = [f for f in flags if f != "--noconfirm"]
        _log.info("[BUILD]", "--interactive: stripped --noconfirm from profile flags")
    if extra_flags:
        flags += extra_flags
        _log.info("[BUILD]", f"Appending CLI flags: {extra_flags}")
    cmd = ["makepkg", "-p", pkgbuild_path.name] + flags

    _log.info("[BUILD]", f"Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}")

    if _log.get_verbosity() >= 3 and not interactive:
        # Capture stdout+stderr and log each line with [MAKEPKG] tag.
        # debug() always writes to the log file, so this fills the gap in the
        # per-package log regardless of terminal verbosity.
        proc = subprocess.Popen(
            cmd, cwd=build_dir, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
            preexec_fn=lift_for_child,
        )
        failed_stage = None
        missing_deps: list[str] = []
        for line in proc.stdout:
            stripped = line.rstrip()
            if "A failure occurred in prepare()." in stripped:
                failed_stage = "prepare"
            elif "A failure occurred in build()." in stripped:
                failed_stage = "build"
            elif "A failure occurred in package()." in stripped:
                failed_stage = "package"
            elif "target not found:" in stripped:
                missing_deps.append(stripped.strip())
            _log.debug("[MAKEPKG]", stripped)
        proc.wait()
        returncode = proc.returncode
    else:
        returncode = subprocess.run(cmd, cwd=build_dir, env=env,
                                   preexec_fn=lift_for_child).returncode
        failed_stage = None
        missing_deps = []

    if returncode != 0:
        # Exit code 8 = E_INSTALL_FAILED (pacman failed to install deps).
        # Also triggered when we collected explicit "target not found" lines.
        if returncode == 8 or missing_deps:
            _log.error("[BUILD]", "Dependency resolution failed.")
            for dep in missing_deps:
                _log.error("[BUILD]", f"  {dep}")
            _log.warn("[BUILD]",
                "This usually means related PKGBUILDs are at different versions. "
                "Run 'git pull --rebase' in each package directory to sync them, "
                "then retry with -m '-f' to force a rebuild.")
        elif failed_stage == "prepare":
            _log.info("[BUILD]", "prepare() failed — likely an upstream issue "
                      "(patch conflict, changed upstream state, or fetch error); "
                      "sysforge does not modify prepare()")
        elif failed_stage == "build":
            _log.info("[BUILD]", "build() failed — could be upstream or a flag/toolchain "
                      "incompatibility from the active sysforge profile")
        elif failed_stage == "package":
            _log.info("[BUILD]", "package() failed — likely an upstream issue; "
                      "sysforge does not modify package()")
        else:
            _log.info("[BUILD]", "re-run with -vvv to capture full makepkg output "
                      "in the log for diagnosis")
        raise subprocess.CalledProcessError(returncode, "makepkg")


def _find_built_packages(build_dir: Path) -> list:
    """Return .pkg.tar.* files in build_dir (excludes .sig files)."""
    return [p for p in Path(build_dir).glob("*.pkg.tar.*")
            if not p.name.endswith(".sig")]


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile,
                       extra_env=None, extra_flags=None, interactive=False):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.

    If makepkg fails but .pkg.tar.* files are present in the build dir, the
    build itself likely succeeded and only the install step failed (e.g. due
    to a sudo timeout). In that case the user is offered a sudo re-auth +
    direct pacman -U path instead of a full rebuild.
    """
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                           extra_env, extra_flags, interactive)
        except subprocess.CalledProcessError as e:
            _log.error("[BUILD]", f"Build failed in batch mode, aborting: {e}")
            raise RuntimeError(f"[build_failed] {e}")
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                               extra_env, extra_flags, interactive)
                break
            except subprocess.CalledProcessError as e:
                _log.error("[BUILD]", f"Build failed: {e}")
                _log.info("[BUILD]", f"PKGBUILD location: {pkgbuild_path}")

                installing = extra_flags and any(
                    f in ("--install", "-i") for f in extra_flags
                )
                built_pkgs = (
                    _find_built_packages(Path(pkgbuild_path).resolve().parent)
                    if installing else []
                )
                if built_pkgs:
                    _log.ui("[BUILD]",
                            "Built packages found — build likely succeeded but "
                            "install failed (sudo timeout?):")
                    for p in built_pkgs:
                        _log.ui("[BUILD]", f"  {p.name}")
                    response = (
                        input(
                            _log.prompt_prefix("UI", "[BUILD]") +
                            "[s]udo re-auth and install, fix PKGBUILD and press "
                            "Enter to retry, or type 'abort' to stop: "
                        )
                        .strip()
                        .lower()
                    )
                    if response == "s":
                        while True:
                            _log.ui("[BUILD]", "Refreshing sudo credentials...")
                            subprocess.run(["sudo", "-v"])
                            result = subprocess.run(
                                ["sudo", "pacman", "-U", "--noconfirm"]
                                + [str(p) for p in built_pkgs]
                            )
                            if result.returncode == 0:
                                _log.ui("[BUILD]", "Install succeeded.")
                                return
                            _log.error("[BUILD]",
                                       f"pacman -U failed (exit {result.returncode})")
                            retry = (
                                input(
                                    _log.prompt_prefix("UI", "[BUILD]") +
                                    "Retry install? [s]udo re-auth again, or 'abort': "
                                )
                                .strip()
                                .lower()
                            )
                            if retry != "s":
                                raise RuntimeError(
                                    "[build_failed] Aborted by user after install failure"
                                )
                    elif response == "abort":
                        raise RuntimeError(
                            "[build_failed] Aborted by user after build failure"
                        )
                    # anything else: fall through to retry the full build
                    _log.info("[BUILD]", "Retrying build...")
                else:
                    response = (
                        input(
                            _log.prompt_prefix("UI", "[BUILD]") +
                            "Manually correct the PKGBUILD and press Enter to retry, "
                            "or type 'abort' to stop: "
                        )
                        .strip()
                        .lower()
                    )
                    if response == "abort":
                        raise RuntimeError(
                            "[build_failed] Aborted by user after build failure"
                        )
                    _log.info("[BUILD]", "Retrying build...")


def _pkgname_from_meta(pkgmeta: dict | None) -> str:
    """Extract a display name from parsed PKGBUILD metadata."""
    if pkgmeta is None:
        return "unknown"
    globals_ = pkgmeta.get("globals", {})
    name = globals_.get("pkgbase") or globals_.get("pkgname", "unknown")
    if isinstance(name, list):
        name = name[0] if name else "unknown"
    return name or "unknown"


def _run_build(pkgbuild_path, resolved_profile, config, groups,
               active_consumes=None, extracted_profile=None, pkgmeta=None,
               extra_flags=None, interactive=False,
               cc_override=None, cxx_override=None, ld_override=None,
               kernel_build: bool = False,
               compiler_flags_extra: str | None = None,
               strip_full_lto: bool = False):
    """
    Emit makepkg.conf and invoke makepkg, handling build failures.

    If extracted_profile is provided (patch_pkgbuild or kernel mode), applies
    the patched PKGBUILD instead of the original. Cleans up patch artifacts on
    success; leaves them in place on failure for diagnosis.

    kernel_build: enables kernel-specific behaviour —
      - Interactive kconfig targets are replaced with olddefconfig unless
        interactive=True, in which case the PKGBUILD runs as-is.
      - Compiler flag keys (CFLAGS/CXXFLAGS/LDFLAGS/etc.) are stripped from
        the emitted makepkg.conf; system conf values pass through verbatim.
      - If the effective CC resolves to clang, LLVM=1 and LLVM_IAS=1 are
        injected into the build environment.
    """
    if extracted_profile is not None:
        # patch_pkgbuild / kernel mode: use patched copy with flags stripped
        pkgbuild_path = apply_patch_pkgbuild(pkgbuild_path, pkgmeta or {"globals": {}})
    else:
        pkgbuild_path = patch_pkgbuild_groups(pkgbuild_path, groups)

    if kernel_build and not interactive:
        patch_noninteractive_kconfig(pkgbuild_path)

    # Probe ThinLTO cache dir (informational, once per build)
    ldflags = resolved_profile.get("LDFLAGS", "")
    if ldflags:
        thinlto = probe_thinlto_cache(ldflags)
        if thinlto:
            if thinlto["exists"]:
                from sysforge.primitives.cache_probe import _fmt_bytes
                _log.info("[CACHE]", f"ThinLTO cache: {_fmt_bytes(thinlto['size_bytes'])} "
                          f"in {thinlto['files']} files ({thinlto['path']})")
            else:
                _log.info("[CACHE]", f"ThinLTO cache dir configured but not yet created: {thinlto['path']}")

    # Snapshot cache state before build for per-build delta
    before_cc = probe_ccache()
    before_sc = probe_sccache()

    success = False
    try:
        extra_env = resolve_env_vars(resolved_profile, active_consumes)

        if kernel_build:
            effective_cc = (
                cc_override
                or resolved_profile.get("CC")
                or os.environ.get("CC", "")
            )
            compiler_name = Path(effective_cc).name if effective_cc else ""
            if compiler_name.startswith("clang"):
                extra_env.update({"LLVM": "1", "LLVM_IAS": "1"})
                _log.info("[KERNEL]", f"Detected clang ({effective_cc!r}): injecting LLVM=1 LLVM_IAS=1")
            else:
                _log.info("[KERNEL]", f"Non-clang toolchain ({effective_cc!r} → 'gcc'): GCC kernel build")

        with emit_makepkg_conf(resolved_profile, active_consumes,
                               cc_override=cc_override,
                               cxx_override=cxx_override,
                               ld_override=ld_override,
                               kernel_build=kernel_build,
                               compiler_flags_extra=compiler_flags_extra,
                               strip_full_lto=strip_full_lto) as conf_path:
            _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile,
                               extra_env, extra_flags, interactive)

        # Post-build cache delta
        pkgname = _pkgname_from_meta(pkgmeta)
        after_cc = probe_ccache()
        after_sc = probe_sccache()
        cc_delta = diff_ccache(before_cc, after_cc) if before_cc is not None and after_cc is not None else None
        sc_delta = diff_sccache(before_sc, after_sc) if before_sc is not None and after_sc is not None else None
        emit_build_stats(pkgname, cc_delta, sc_delta)
        record_build_result(pkgname, cc_delta, sc_delta)

        success = True
    except RuntimeError:
        raise
    except Exception as e:
        handle_failure("tempfile_write_failed", str(e), config)
    finally:
        if extracted_profile is not None:
            if success:
                cleanup_patch_artifacts(pkgbuild_path.parent / "PKGBUILD")
            else:
                artifacts = "PKGBUILD.sysforge"
                if extracted_profile:
                    artifacts += " and pkgbuild_extracted_profile.toml"
                _log.warn("[PATCH]", f"Build failed — leaving {artifacts} in place for diagnosis")
        else:
            if success:
                if pkgbuild_path.exists():
                    pkgbuild_path.unlink()
                    _log.info("[BUILD]", f"Removed patched PKGBUILD: {pkgbuild_path}")
            else:
                _log.warn("[BUILD]", f"Build failed — leaving patched PKGBUILD in place: {pkgbuild_path}")


# ---------------------------------------------------------------------------
# Build mode detection
# ---------------------------------------------------------------------------

def _get_build_mode(matched_rules, config):
    """
    Return the build_mode from the winning rule's profile chain without
    performing a full profile resolution (and without logging).

    Walks the extends chain of the winning profile looking for a build_mode
    key. Returns None if no build_mode is found or no rules matched.
    """
    profiles = config.get("profiles", {})
    defaults = config.get("defaults", {})

    winner = None
    for rule in matched_rules:
        if "profile" not in rule:
            continue
        if winner is None or rule.get("priority", 0) > winner.get("priority", 0):
            winner = rule

    profile_name = winner["profile"] if winner else defaults.get("profile", "bare")

    visited: set[str] = set()
    while profile_name and profile_name not in visited:
        visited.add(profile_name)
        p = profiles.get(profile_name, {})
        if "build_mode" in p:
            return p["build_mode"]
        profile_name = p.get("extends")

    return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(pkgbuild_path, extra_flags=None, interactive=False,
        pkg_log: bool = True, persist_log: bool = False,
        log_dir=None, profile_conf=None,
        cc_override=None, cxx_override=None, ld_override=None,
        cache_report: bool = False, init_session: bool = True,
        update: bool = True,
        profile_override: str | None = None,
        compiler_flags_extra: str | None = None,
        strip_full_lto: bool = False,
        state_dir=None):
    config_paths = [Path(profile_conf)] if profile_conf is not None else None
    config = load_config(config_paths=config_paths)
    # Note: load_config() debug dumps (full flag_profiles etc.) fire here, before the
    # pkg log is open — they go to the unified log (if open) and terminal only.

    try:
        pkgbuild_path = find_pkgbuild(str(pkgbuild_path), config)
    except FileNotFoundError as e:
        print(f"[SYSFORGE] Error: {e}", file=sys.stderr)
        sys.exit(1)

    # Open per-package log as early as possible so subsequent debug output is captured.
    # Use the PKGBUILD directory name; this matches pkgbase in all normal cases.
    if pkg_log:
        log_base = Path(log_dir) if log_dir is not None else pkgbuild_path.parent
        log_path = log_base / f"sysforge_{pkgbuild_path.parent.name}.log"
        _log.open_pkg_log(log_path, argv=sys.argv)
        _log.info("[BUILD]", f"Per-package log: {log_path}")

    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    if init_session:
        reset_session()
        emit_system_probes()

    if update:
        try:
            git_pull_rebase(pkgbuild_path.parent)
        except RuntimeError as e:
            _log.error("[GIT]", str(e))
            sys.exit(1)
    else:
        _log.info("[BUILD]", "--no-update: skipping git pull --rebase")

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        handle_failure("pkgbuild_unparseable", str(e), config)
        pkgmeta = {"globals": {}}

    build_success = False
    try:
        matched_rules = match_rules(pkgmeta, config.get("rules", []))

        if profile_override is not None:
            # Bypass rule matching: resolve the named profile directly.
            from sysforge.primitives.profile import merge_extends
            profiles = config.get("profiles", {})
            if profile_override not in profiles:
                raise RuntimeError(
                    f"[BUILD] profile_override {profile_override!r} not found in loaded config"
                )
            resolved_profile = merge_extends(profile_override, profiles, conflict_groups=conflict_groups)
            build_mode = resolved_profile.get("build_mode")
            _log.info("[BUILD]", f"Profile override: {profile_override!r} (build_mode={build_mode!r})")
        else:
            build_mode = _get_build_mode(matched_rules, config)
            resolved_profile = None  # resolved below after extracted_profile is known

        kernel_build = (build_mode == "kernel")

        extracted_profile = None
        if build_mode in ("patched_pkgbuild", "kernel"):
            extracted_profile = extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
            if extracted_profile:
                write_extracted_profile(extracted_profile, pkgbuild_path)

        if profile_override is None:
            resolved_profile = resolve_profile(
                pkgmeta, matched_rules, config, conflict_groups,
                extracted_profile=extracted_profile,
            )
        active_consumes = resolve_consumes(resolved_profile, pkgmeta, inference_map)
        groups = resolve_groups(pkgmeta, matched_rules, config.get("defaults", {}))

        if resolved_profile.get("clean_builddir", False):
            build_dir = pkgbuild_path.parent
            for entry in build_dir.iterdir():
                if entry.name != "PKGBUILD" and not entry.name.endswith(".PKGBUILD"):
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
            _log.info("[BUILD]", f"Cleaned build dir: {build_dir}")

        import_pgp_keys(pkgmeta, pkgbuild_path)
        run_dep_analysis(pkgmeta, config)
        _run_build(
            pkgbuild_path, resolved_profile, config, groups,
            active_consumes=active_consumes,
            extracted_profile=extracted_profile if build_mode in ("patched_pkgbuild", "kernel") else None,
            pkgmeta=pkgmeta,
            extra_flags=extra_flags,
            interactive=interactive,
            cc_override=cc_override,
            cxx_override=cxx_override,
            ld_override=ld_override,
            kernel_build=kernel_build,
            compiler_flags_extra=compiler_flags_extra,
            strip_full_lto=strip_full_lto,
        )
        build_success = True

        # Record build metadata for `sysforge update` (non-fatal)
        try:
            from sysforge.primitives.build_state import BuildState
            from sysforge.pipeline.state import resolve_state_dir
            _state_dir, _ = resolve_state_dir(state_dir)
            bs = BuildState(_state_dir)
            globals_ = pkgmeta.get("globals", {})
            pkgnames = globals_.get("pkgname", [])
            if isinstance(pkgnames, str):
                pkgnames = [pkgnames]
            pkgbase = globals_.get("pkgbase") or (pkgnames[0] if pkgnames else "unknown")
            fs = serialize_flags(resolved_profile) if resolved_profile is not None else None
            for name in pkgnames:
                bs.record(
                    pkgname=name,
                    pkgver=globals_.get("pkgver", ""),
                    pkgrel=globals_.get("pkgrel", "1"),
                    epoch=globals_.get("epoch", "0"),
                    pkgbase=pkgbase,
                    pkgbuild_dir=pkgbuild_path.parent,
                    build_mode="profiled",
                    flags_string=fs,
                )
            bs.save()
            _log.info("[BUILD]", f"Recorded build state for {pkgbase!r}")
        except Exception as e:
            _log.warn("[BUILD]", f"Failed to record build state: {e}")

    finally:
        if pkg_log:
            _log.close_pkg_log(success=build_success, persist=persist_log)

    if cache_report:
        emit_session_report()


if __name__ == "__main__":
    run(sys.argv[1])
