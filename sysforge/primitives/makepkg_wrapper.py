"""
makepkg_wrapper.py — build execution

Responsible for conf file emission, makepkg invocation, retry logic,
and the top-level run() entry point. All config loading, profile resolution,
and PKGBUILD parsing are delegated to their respective modules.

Public API:
    BuildOptions                       — dataclass of run() options (all fields defaulted)
    PGOBuildSkipped                    — raised when a pgo_llvm_toolchain build is skipped
    expand_makepkg_flags(flags_str)   → list
    run(pkgbuild_path, options=None)
"""
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from sysforge.primitives.resource_guard import lift_for_child

from sysforge.primitives.config import (
    find_pkgbuild,
    load_config,
    load_conflict_groups,
    load_consumes_inference,
    parse_system_makepkg_conf,
)
from sysforge.primitives.paths import TOOLCHAIN_PATH
from sysforge.primitives.pkgbuild_meta import has_hardcoded_gcc, parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import (
    apply_patch_pkgbuild,
    cleanup_patch_artifacts,
    extract_pkgbuild_profile,
    patch_noninteractive_kconfig,
    patch_pkgbuild_groups,
    patch_subshell_env_reset,
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
from sysforge import log
_abi_log     = log.get_logger("ABI")
_build_log   = log.get_logger("BUILD")
_cache_log   = log.get_logger("CACHE")
_conf_log    = log.get_logger("CONF")
_env_log     = log.get_logger("ENV")
_flag_log    = log.get_logger("FLAG")
_git_log     = log.get_logger("GIT")
_kernel_log  = log.get_logger("KERNEL")
_makepkg_log = log.get_logger("MAKEPKG")
_patch_log   = log.get_logger("PATCH")
_pgo_log     = log.get_logger("PGO")
from sysforge.primitives.profile import (
    CONF_KEY_MAP,
    KERNEL_CLEAN_KEYS,
    SYSFORGE_KEYS,
    get_build_mode,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
    serialize_flags,
)


# ---------------------------------------------------------------------------
# Flag string utilities
# ---------------------------------------------------------------------------

def expand_makepkg_flags(flags_str) -> list:
    """
    Split a makepkg flags string into a list of individual flags,
    expanding combined short flags like '-sfci' into ['-s', '-f', '-c', '-i'].
    Long flags (--noconfirm) and flags with values are passed through as-is.
    """
    if not flags_str:
        return []
    result = []
    for token in flags_str.split():
        if token.startswith("--"):
            result.append(token)
        elif token.startswith("-") and len(token) > 2:
            # Combined short flags e.g. -sfci → -s -f -c -i
            result.extend(f"-{ch}" for ch in token[1:])
        else:
            result.append(token)
    return result


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

# Substrings in makepkg output that identify a clang-only flag rejected by
# GCC. When any of these appears on stderr/stdout alongside a non-zero exit,
# invoke_makepkg raises ToolchainMismatchError so _run_build can retry once
# with the GCC flag guard forced on.
TOOLCHAIN_MISMATCH_PATTERNS = (
    "unrecognized argument to '-flto=' option",
    "unrecognized command-line option '-flto=thin'",
)


class ToolchainMismatchError(subprocess.CalledProcessError):
    """
    Raised by invoke_makepkg when the makepkg failure was caused by the
    active profile's compiler flags being incompatible with the actual
    compiler the package's build system invoked — most commonly, clang-only
    flags like -flto=thin fed to a hardcoded g++.

    Distinct from CalledProcessError so _run_build can catch it specifically
    and trigger an automatic retry with the GCC+LTO flag guard forced on,
    without prompting the user for manual correction.
    """



def _detect_linker_from_ldflags(ldflags_val):
    """Return the linker name declared by -fuse-ld=X in LDFLAGS, or None."""
    for token in ldflags_val.split():
        if token.startswith("-fuse-ld="):
            return token[len("-fuse-ld="):]
    return None


def _detect_linker_from_rustflags(rustflags_val):
    """Return the linker name declared by -C link-arg=-fuse-ld=X in RUSTFLAGS, or None."""
    tokens = rustflags_val.split()
    for i, token in enumerate(tokens):
        # Handle both "-C link-arg=-fuse-ld=X" (two tokens) and
        # "-Clink-arg=-fuse-ld=X" (single token)
        if token == "-C" and i + 1 < len(tokens):
            arg = tokens[i + 1]
            if arg.startswith("link-arg=-fuse-ld="):
                return arg[len("link-arg=-fuse-ld="):]
        elif token.startswith("-Clink-arg=-fuse-ld="):
            return token[len("-Clink-arg=-fuse-ld="):]
    return None


def _replace_rustflags_linker(rustflags_val, new_linker):
    """Replace -C link-arg=-fuse-ld=X in RUSTFLAGS with a new linker."""
    tokens = rustflags_val.split()
    out = []
    i = 0
    while i < len(tokens):
        if tokens[i] == "-C" and i + 1 < len(tokens) and tokens[i + 1].startswith("link-arg=-fuse-ld="):
            out.append("-C")
            out.append(f"link-arg=-fuse-ld={new_linker}")
            i += 2
        elif tokens[i].startswith("-Clink-arg=-fuse-ld="):
            out.append(f"-Clink-arg=-fuse-ld={new_linker}")
            i += 1
        else:
            out.append(tokens[i])
            i += 1
    return " ".join(out)


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
                _flag_log.info(f"Replaced {tokens[i]} with {new_token} (--ld override)")
            tokens[i] = new_token
            return " ".join(tokens)
    _flag_log.info(f"Injected {new_token} into LDFLAGS (--ld override)")
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
                      linker_flags_extra: str | None = None,
                      strip_full_lto: bool = False,
                      pkgbuild_has_hardcoded_gcc: bool = False,
                      reactive_gcc_fallback: bool = False):
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

    kernel_build: when True, KERNEL_CLEAN_KEYS (CFLAGS, CXXFLAGS, LDFLAGS,
    CPPFLAGS, DEBUG_*) are excluded from profile overrides and ld_override is
    ignored. System conf values for those keys pass through verbatim.

    compiler_flags_extra: when set, appended verbatim to CFLAGS and LDFLAGS in
    the emitted conf after all other processing (including the linker guard).
    CXXFLAGS is skipped when it references $CFLAGS (flags are inherited via shell
    expansion; injecting separately would cause duplication). Intended for PGO
    generate/use flags injected by the toolchain stage.

    strip_full_lto: when True, strips -flto/-flto=full from CFLAGS/CXXFLAGS/LDFLAGS
    and clears LTOFLAGS so makepkg's lto option cannot re-inject ThinLTO flags at
    build time. Disables LTO entirely for PGO passes — ThinLTO link-time codegen
    combined with IR PGO causes non-PIC vtable relocations in shared library builds.

    pkgbuild_has_hardcoded_gcc: proactive signal that the PKGBUILD's build()
    or package() function invokes gcc/g++ directly (rather than honoring $CC/
    $CXX). Activates the GCC+LTO flag guard even when the configured CC is
    clang — rewriting clang-only flags like -flto=thin so the hardcoded GCC
    does not reject them.

    reactive_gcc_fallback: set by _run_build() on the second pass of an
    auto-retry after the first build failed with a GCC-rejects-clang-flag
    error. Activates the same GCC+LTO guard as pkgbuild_has_hardcoded_gcc,
    distinguished only for log-message clarity.
    """
    env_keys = CONF_KEY_MAP.get("env", set())
    # Toolchain keys (CC, CXX) are delivered via subprocess env, not via the
    # conf file. makepkg sources the conf as a shell script, so any CC/CXX
    # present in the system conf would overwrite the env-injected values.
    # Exclude them from conf output alongside env-type keys.
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    conf_exclude_keys = env_keys | toolchain_keys

    if active_consumes is None:
        allowed_keys = None
    else:
        allowed_keys: set[str] = set()
        for conf_type in active_consumes:
            if conf_type == "env":
                continue
            if conf_type in CONF_KEY_MAP:
                allowed_keys.update(CONF_KEY_MAP[conf_type])

    # Profile keys to write: filter out internal, env-pass, out-of-consumes,
    # and (for kernel builds) compiler flag keys.
    profile_overrides = {}
    for key, val in resolved_profile.items():
        if key in SYSFORGE_KEYS:
            continue
        if key in env_keys:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            continue
        if kernel_build and key in KERNEL_CLEAN_KEYS:
            continue
        profile_overrides[key] = val

    if kernel_build:
        _kernel_log.info("Kernel build: omitting profile flag keys from makepkg.conf "
                  "(CFLAGS/CXXFLAGS/LDFLAGS/CPPFLAGS/DEBUG_*); system conf values preserved")

    # Load system conf baseline
    system_assignments = parse_system_makepkg_conf(system_conf_path)

    # Apply CLI toolchain overrides on top of profile values
    if cc_override is not None:
        profile_overrides["CC"] = cc_override
        _flag_log.info(f"CC overridden via --cc: {cc_override}")
    if cxx_override is not None:
        profile_overrides["CXX"] = cxx_override
        _flag_log.info(f"CXX overridden via --cxx: {cxx_override}")
    if ld_override is not None:
        if kernel_build:
            _kernel_log.info(f"--ld={ld_override!r} ignored for kernel build (use LLVM=1 for lld)")
        else:
            current_ldflags = profile_overrides.get("LDFLAGS", "")
            profile_overrides["LDFLAGS"] = _inject_linker(current_ldflags, ld_override)

    # Linker guard: determine the effective linker. If no -fuse-ld=X is declared,
    # the effective linker is the system default (bfd). Strip lld-specific flags
    # whenever the effective linker is not lld — not only when a linker is declared
    # but missing, since undeclared LDFLAGS containing lld-only flags will break
    # configure test compilations against the system linker.
    effective_linker = "ld"
    if "LDFLAGS" in profile_overrides:
        declared_linker = _detect_linker_from_ldflags(profile_overrides["LDFLAGS"])
        effective_linker = declared_linker or "ld"

        if declared_linker and not shutil.which(declared_linker):
            _flag_log.warn(f"Declared linker '{declared_linker}' not found on PATH — treating effective linker as 'ld'")
            effective_linker = "ld"

        if effective_linker != "lld":
            cleaned, stripped_tokens = _strip_lld_flags(profile_overrides["LDFLAGS"])
            if stripped_tokens:
                _flag_log.warn(f"Effective linker is '{effective_linker}' (not lld) — stripping lld-specific flags from LDFLAGS")
                for tok in stripped_tokens:
                    _flag_log.warn(f"Stripped lld-only flag: {tok}")
                profile_overrides["LDFLAGS"] = cleaned

        # RUSTFLAGS linker reconciliation: if RUSTFLAGS declares a different
        # linker than LDFLAGS, override it to match. A mismatch causes link
        # failures when LTO is enabled — mold cannot resolve LLVM bitcode
        # produced by lld, and vice versa.
        if "RUSTFLAGS" in profile_overrides:
            rust_linker = _detect_linker_from_rustflags(profile_overrides["RUSTFLAGS"])
            if rust_linker and rust_linker != effective_linker:
                _flag_log.warn(
                    f"RUSTFLAGS linker '{rust_linker}' conflicts with "
                    f"LDFLAGS effective linker '{effective_linker}' — "
                    f"overriding RUSTFLAGS to use '{effective_linker}'")
                profile_overrides["RUSTFLAGS"] = _replace_rustflags_linker(
                    profile_overrides["RUSTFLAGS"], effective_linker)

    # GCC + LTO guard: GCC emits .gnu.lto_* bitcode that only GNU ld/gold can
    # process. When the effective compiler is GCC — either because the profile
    # sets CC=gcc, or because the PKGBUILD's build() hardcodes gcc/g++, or a
    # prior build attempt failed with a clang-only flag rejected by GCC:
    #   - Rewrite -flto=thin → -flto (thin LTO is clang-only)
    #   - If the effective linker is lld, disable LTO entirely — lld cannot
    #     process GCC LTO bitcode objects (undefined symbol errors at link time)
    effective_cc = cc_override or resolved_profile.get("CC")
    _profile_is_gcc = effective_cc and not effective_cc.startswith("clang")
    _is_gcc = (
        _profile_is_gcc
        or pkgbuild_has_hardcoded_gcc
        or reactive_gcc_fallback
    )
    if _profile_is_gcc:
        _gcc_reason = f"CC is '{effective_cc}' (not clang)"
    elif pkgbuild_has_hardcoded_gcc:
        _gcc_reason = "PKGBUILD build()/package() invokes hardcoded gcc/g++"
    elif reactive_gcc_fallback:
        _gcc_reason = "post-failure fallback (GCC rejected clang-only flag)"
    else:
        _gcc_reason = None
    if _is_gcc:
        _thin_lto_rewritten = False
        for key in ("LTOFLAGS", "CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                val = profile_overrides[key]
            elif key in system_assignments:
                raw = system_assignments[key].strip()
                val = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
            else:
                continue
            if "-flto=thin" in val:
                profile_overrides[key] = val.replace("-flto=thin", "-flto")
                _thin_lto_rewritten = True
        if _thin_lto_rewritten:
            _flag_log.warn(
                f"{_gcc_reason} — rewriting -flto=thin to -flto "
                f"(GCC does not support thin LTO)")

    if _is_gcc and effective_linker == "lld":
        # GCC LTO bitcode (.gnu.lto_* sections) is incompatible with lld.
        # Disable LTO fully: clear LTOFLAGS, strip -flto* from flag keys, and
        # flip 'lto' → '!lto' in OPTIONS so makepkg's lto buildenv hook doesn't
        # re-inject -flto via the ${LTOFLAGS:--flto} fallback.
        profile_overrides["LTOFLAGS"] = ""
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                val = profile_overrides[key]
            elif key in system_assignments:
                raw = system_assignments[key].strip()
                val = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
            else:
                continue
            cleaned, stripped = _strip_full_lto(val)
            if stripped:
                profile_overrides[key] = cleaned
        # Flip lto → !lto in OPTIONS (may come from profile or system conf).
        options_raw = profile_overrides.get("OPTIONS") or system_assignments.get("OPTIONS", "")
        if options_raw:
            options_raw = options_raw.strip()
            # OPTIONS is a bash array: (token token ...) — work inside the parens
            inner = options_raw.strip("()")
            tokens = inner.split()
            new_tokens = ["!lto" if t == "lto" else t for t in tokens]
            profile_overrides["OPTIONS"] = "(" + " ".join(new_tokens) + ")"
        _flag_log.warn(
            f"{_gcc_reason} with linker '{effective_linker}' — "
            f"disabling LTO (GCC LTO bitcode is incompatible with lld)")

    # Full LTO stripping for PGO passes. -flto/-flto=full are incompatible with
    # clang IR PGO. -flto=thin is nominally compatible but triggers non-PIC
    # ThinLTO codegen for shared libraries (R_X86_64_PC32 vtable relocations in
    # lld's ThinLTO backend when PGO profile data is applied). Disable entirely.
    if strip_full_lto:
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                cleaned, stripped_toks = _strip_full_lto(profile_overrides[key])
                if stripped_toks:
                    _pgo_log.warn(
                              f"Stripped full-LTO flag(s) from {key} (incompatible with IR PGO): "
                              f"{' '.join(stripped_toks)}")
                    profile_overrides[key] = cleaned
        # Clear LTOFLAGS so makepkg's lto option doesn't re-inject -flto=thin at
        # build time (LTOFLAGS is appended to CFLAGS/CXXFLAGS/LDFLAGS by makepkg
        # when OPTIONS contains lto, bypassing the stripping above).
        profile_overrides["LTOFLAGS"] = ""
        _pgo_log.info("Cleared LTOFLAGS for PGO pass (LTO disabled)")

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
            # Skip CXXFLAGS if it delegates to $CFLAGS. Injecting into both
            # causes the flags to appear twice after shell expansion ($CFLAGS
            # already carries the injected flags; CXXFLAGS inherits them).
            if key == "CXXFLAGS" and "$CFLAGS" in base:
                continue
            profile_overrides[key] = (base + " " + compiler_flags_extra).strip()
        _pgo_log.info(f"Injecting into CFLAGS/CXXFLAGS/LDFLAGS: {compiler_flags_extra!r}")

    # Linker-only flag injection (e.g. profile runtime library for PGO Pass 2).
    # Unlike compiler_flags_extra, these flags only go to LDFLAGS — adding -l/-L
    # flags to CFLAGS/CXXFLAGS is harmless but noisy.
    if linker_flags_extra:
        key = "LDFLAGS"
        if key in profile_overrides:
            base = profile_overrides[key]
        elif key in system_assignments:
            raw = system_assignments[key].strip()
            base = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
        else:
            base = ""
        profile_overrides[key] = (base + " " + linker_flags_extra).strip()
        _pgo_log.info(f"Injecting into LDFLAGS: {linker_flags_extra!r}")

    # Build output lines: system conf keys in their original raw form,
    # profile-overridden keys substituted inline, new profile keys appended.
    conf_lines = ["# Generated by SysForge — merged system conf + profile overrides"]

    for key, raw_val in system_assignments.items():
        if key in conf_exclude_keys:
            # Env/toolchain keys are injected via subprocess env, never written
            # to conf — even if the system conf has them set. makepkg sources
            # the conf as a shell script, so values here would overwrite the
            # env-injected ones.
            continue
        if key in profile_overrides:
            val = profile_overrides[key]
            # Bash array values (OPTIONS, BUILDENV, etc.) must not be quoted.
            if val.startswith("("):
                conf_lines.append(f"{key}={val}")
            else:
                conf_lines.append(f'{key}="{val}"')
        else:
            conf_lines.append(f"{key}={raw_val}")

    new_keys = [k for k in profile_overrides if k not in system_assignments]
    if new_keys:
        conf_lines.append("")
        conf_lines.append("# SysForge profile additions (not in system conf)")
        for key in new_keys:
            val = profile_overrides[key]
            if val.startswith("("):
                conf_lines.append(f"{key}={val}")
            else:
                conf_lines.append(f'{key}="{val}"')

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="sysforge_makepkg_",
        suffix=".conf",
        delete=False,
    ) as f:
        f.write("\n".join(conf_lines) + "\n")
        tmp_path = f.name

    _conf_log.info(f"Wrote temp makepkg.conf: {tmp_path}")
    _conf_log.debug(f"Temp makepkg.conf contents:\n{chr(10).join(conf_lines)}")
    try:
        yield tmp_path
    finally:
        os.unlink(tmp_path)
        _conf_log.info(f"Removed temp makepkg.conf: {tmp_path}")


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
      3. Unknown keys — not in any CONF_KEY_MAP type and not in SYSFORGE_KEYS.
         Always collected and logged under [ENV] as a warning.

    Returns dict[str, str] of key -> value pairs to inject on invocation.
    Empty dict if nothing to inject.
    """
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    env_type_keys  = CONF_KEY_MAP.get("env", set())

    # All keys explicitly classified into any conf type
    all_conf_keys: set[str] = set()
    for keys in CONF_KEY_MAP.values():
        all_conf_keys.update(keys)

    collect_env_type = active_consumes is None or "env" in active_consumes

    result: dict[str, str] = {}
    unknown: list[str] = []

    for key, val in resolved_profile.items():
        if key in SYSFORGE_KEYS:
            continue

        if key in toolchain_keys:
            # Always delivered via env — makepkg doesn't export CC/CXX from conf
            result[key] = val
            _env_log.info(f"Injecting (toolchain): {key}={val!r}")
            continue

        if key in env_type_keys:
            if collect_env_type:
                result[key] = val
                _env_log.info(f"Injecting (env type): {key}={val!r}")
            else:
                _env_log.info(f"Skipping env-type key {key!r} (not in active_consumes)")
            continue

        if key not in all_conf_keys:
            # Unknown key — not classified; env pass with warning
            result[key] = val
            unknown.append(key)

    if unknown:
        _env_log.warn(f"Unclassified profile keys injected via env (consider adding to CONF_KEY_MAP): {sorted(unknown)}")

    return result


def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                   extra_env=None, extra_flags=None, interactive=False,
                   strip_flags=None):
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
        _env_log.info(f"Stripped venv from PATH/VIRTUAL_ENV: {venv_dir}")

    # Strip all makepkg-managed and toolchain keys from the inherited shell env
    # so the temp conf and profile env injection are the sole authority.
    # Without this, shell vars like CC=clang or CFLAGS=... win over what the
    # conf/profile sets, producing unpredictable builds.
    _strip_keys = CONF_KEY_MAP.get("makepkg", set()) | CONF_KEY_MAP.get("toolchain", set())
    for k in sorted(_strip_keys):
        if k in env:
            _env_log.info(f"Stripped from shell env (superseded by profile): {k}={env.pop(k)!r}")

    # LLVM_PROFILE_FILE is only meaningful during PGO pass 2, where the
    # toolchain stage injects it via extra_env.  If inherited from the shell
    # (e.g. user .zprofile), it causes every clang invocation to emit profraw
    # files into an unrelated directory.
    _llvm_pf = env.pop("LLVM_PROFILE_FILE", None)
    if _llvm_pf:
        _env_log.info(f"Stripped inherited LLVM_PROFILE_FILE={_llvm_pf!r} (only set during PGO pass 2)")

    env["MAKEPKG_CONF"] = str(conf_path)

    # Suppress pagers for any subprocess run by PKGBUILDs. libinput-git's
    # meson summary and git log in prepare() both pipe through less(1) and
    # stall unattended batch builds waiting for the user to quit.
    if not interactive:
        env.setdefault("GIT_PAGER", "cat")
        env.setdefault("PAGER", "cat")
        env.setdefault("SYSTEMD_PAGER", "cat")

    if extra_env:
        for k, v in sorted(extra_env.items()):
            if k in env:
                _env_log.warn(f"Overriding shell {k}={env[k]!r} with profile value {v!r}")
        env.update(extra_env)

    flags = list(resolved_profile.get("makepkg_flags", []))
    if interactive:
        flags = [f for f in flags if f != "--noconfirm"]
        _build_log.info("--interactive: stripped --noconfirm from profile flags")
    if extra_flags:
        flags += extra_flags
        _build_log.info(f"Appending CLI flags: {extra_flags}")
    if strip_flags:
        before = flags[:]
        flags = [f for f in flags if f not in strip_flags]
        removed = [f for f in before if f not in flags]
        if removed:
            _build_log.info(f"Batch mode: stripped flags {removed}")
    cmd = ["makepkg", "-p", pkgbuild_path.name] + flags

    _build_log.info(f"Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}")

    # Always capture stdout+stderr so we can classify failures (prepare vs
    # build vs package stages, missing deps) and detect clang→GCC flag
    # rejections that trigger an automatic retry. In verbose mode, lines go
    # through _makepkg_log (prefixed [MAKEPKG][DEBUG] in the log file);
    # otherwise they're forwarded verbatim to stdout so the user still sees
    # live output. stdin is inherited from the parent so interactive prompts
    # (sudo, signing keys) continue to work.
    verbose_log = log.get_verbosity() >= 3
    proc = subprocess.Popen(
        cmd, cwd=build_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
        preexec_fn=lift_for_child,
    )
    failed_stage = None
    missing_deps: list[str] = []
    toolchain_mismatch = False
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
        if not toolchain_mismatch:
            for _pat in TOOLCHAIN_MISMATCH_PATTERNS:
                if _pat in stripped:
                    toolchain_mismatch = True
                    break
        if verbose_log:
            _makepkg_log.debug(stripped)
        else:
            sys.stdout.write(line)
            sys.stdout.flush()
    proc.wait()
    returncode = proc.returncode

    if returncode != 0:
        # Exit code 8 = E_INSTALL_FAILED (pacman failed to install deps).
        # Also triggered when we collected explicit "target not found" lines.
        if returncode == 8 or missing_deps:
            _build_log.error("Dependency resolution failed.")
            for dep in missing_deps:
                _build_log.error(f"  {dep}")
            _build_log.warn(
                "This usually means related PKGBUILDs are at different versions. "
                "Run 'git pull --rebase' in each package directory to sync them, "
                "then retry with -m '-f' to force a rebuild.")
        elif failed_stage == "prepare":
            _build_log.info("prepare() failed — likely an upstream issue "
                      "(patch conflict, changed upstream state, or fetch error); "
                      "sysforge does not modify prepare()")
        elif failed_stage == "build":
            _build_log.info("build() failed — could be upstream or a flag/toolchain "
                      "incompatibility from the active sysforge profile")
        elif failed_stage == "package":
            _build_log.info("package() failed — likely an upstream issue; "
                      "sysforge does not modify package()")
        else:
            _build_log.info("re-run with -vvv to capture full makepkg output "
                      "in the log for diagnosis")
        if toolchain_mismatch:
            _flag_log.warn(
                "Detected clang-only compiler flag rejected by GCC — "
                "the package's build system likely invokes a hardcoded gcc/g++"
            )
            raise ToolchainMismatchError(returncode, "makepkg")
        raise subprocess.CalledProcessError(returncode, "makepkg")


def _find_built_packages(build_dir: Path) -> list:
    """Return .pkg.tar.* files in build_dir (excludes .sig files)."""
    return [p for p in Path(build_dir).glob("*.pkg.tar.*")
            if not p.name.endswith(".sig")]


_PKG_FILENAME_EXT = re.compile(r"\.pkg\.tar\.[^.]+$")


def _parse_built_pkg_filename(pkgname: str, filename: str) -> tuple[str, str, str] | None:
    """
    Parse a built Arch package filename into ``(epoch, pkgver, pkgrel)``.

    Expected form: ``<pkgname>-[epoch:]<pkgver>-<pkgrel>-<arch>.pkg.tar.<ext>``.
    Returns None if the filename does not match this layout for ``pkgname``.

    This is the canonical post-build source of truth for a package's version:
    the filename always carries the fully resolved values, whereas the static
    PKGBUILD parser intentionally leaves shell parameter-expansion forms like
    ``${_ver/[a-z]/.${_ver//[0-9.]/}}`` untouched. Anchoring on the known
    ``pkgname`` is required because pkgnames may themselves contain hyphens
    (e.g. ``openssl-1.1``).
    """
    m = _PKG_FILENAME_EXT.search(filename)
    if not m:
        return None
    stem = filename[:m.start()]
    prefix = pkgname + "-"
    if not stem.startswith(prefix):
        return None
    rest = stem[len(prefix):]
    try:
        ver_rel, _arch = rest.rsplit("-", 1)
        ver_part, pkgrel = ver_rel.rsplit("-", 1)
    except ValueError:
        return None
    epoch = "0"
    if ":" in ver_part:
        epoch, _, ver_part = ver_part.partition(":")
    if not ver_part or not pkgrel:
        return None
    return (epoch, ver_part, pkgrel)


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile,
                       extra_env=None, extra_flags=None, interactive=False,
                       strip_flags=None):
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
                           extra_env, extra_flags, interactive, strip_flags)
        except ToolchainMismatchError:
            # Propagate so _run_build can auto-retry with the GCC flag guard
            # before falling back to normal batch-mode failure handling.
            raise
        except subprocess.CalledProcessError as e:
            _build_log.error(f"Build failed in batch mode, aborting: {e}")
            raise RuntimeError(f"[build_failed] {e}")
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                               extra_env, extra_flags, interactive, strip_flags)
                break
            except ToolchainMismatchError:
                # Propagate so _run_build can auto-retry with the GCC flag
                # guard instead of prompting the user for manual correction.
                raise
            except subprocess.CalledProcessError as e:
                _build_log.error(f"Build failed: {e}")
                _build_log.info(f"PKGBUILD location: {pkgbuild_path}")

                installing = extra_flags and any(
                    f in ("--install", "-i") for f in extra_flags
                )
                built_pkgs = (
                    _find_built_packages(Path(pkgbuild_path).resolve().parent)
                    if installing else []
                )
                if built_pkgs:
                    _build_log.ui(
                            "Built packages found — build likely succeeded but "
                            "install failed (sudo timeout?):")
                    for p in built_pkgs:
                        _build_log.ui(f"  {p.name}")
                    from sysforge.ui import progress as _ui_progress
                    _ui_progress.clear()
                    response = (
                        input(
                            _build_log.prompt_prefix("UI") +
                            "[s]udo re-auth and install, fix PKGBUILD and press "
                            "Enter to retry, or type 'abort' to stop: "
                        )
                        .strip()
                        .lower()
                    )
                    if response == "s":
                        while True:
                            _build_log.ui("Refreshing sudo credentials...")
                            subprocess.run(["sudo", "-v"])
                            result = subprocess.run(
                                ["sudo", "pacman", "-U", "--noconfirm"]
                                + [str(p) for p in built_pkgs]
                            )
                            if result.returncode == 0:
                                _build_log.ui("Install succeeded.")
                                return
                            _build_log.error(
                                       f"pacman -U failed (exit {result.returncode})")
                            from sysforge.ui import progress as _ui_progress
                            _ui_progress.clear()
                            retry = (
                                input(
                                    _build_log.prompt_prefix("UI") +
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
                    _build_log.info("Retrying build...")
                else:
                    from sysforge.ui import progress as _ui_progress
                    _ui_progress.clear()
                    response = (
                        input(
                            _build_log.prompt_prefix("UI") +
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
                    _build_log.info("Retrying build...")


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
               linker_flags_extra: str | None = None,
               strip_full_lto: bool = False,
               injected_env: dict | None = None,
               strip_flags=None,
               pkgbuild_has_hardcoded_gcc: bool = False):
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

    # Reset toolchain env vars in subshell functions so sub-builds (musl
    # bootstrap, embedded grub, etc.) use the system default compiler/linker
    # instead of inheriting the sysforge profile CC/CXX or shell LD.
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    toolchain_env = {k: v for k, v in resolved_profile.items() if k in toolchain_keys}
    inherited_env = {k: v for k, v in os.environ.items() if k in ("CC", "CXX", "LD")}
    patch_subshell_env_reset(pkgbuild_path, toolchain_env, inherited_env=inherited_env)

    # Probe ThinLTO cache dir (informational, once per build)
    ldflags = resolved_profile.get("LDFLAGS", "")
    if ldflags:
        thinlto = probe_thinlto_cache(ldflags)
        if thinlto:
            if thinlto["exists"]:
                from sysforge.primitives.cache_probe import _fmt_bytes
                _cache_log.info(f"ThinLTO cache: {_fmt_bytes(thinlto['size_bytes'])} "
                          f"in {thinlto['files']} files ({thinlto['path']})")
            else:
                _cache_log.info(f"ThinLTO cache dir configured but not yet created: {thinlto['path']}")

    # Snapshot cache state before build for per-build delta
    before_cc = probe_ccache()
    before_sc = probe_sccache()

    success = False
    try:
        extra_env = resolve_env_vars(resolved_profile, active_consumes)
        # CLI toolchain overrides must also land in the subprocess env directly.
        # emit_makepkg_conf writes them to the conf file, but that only affects
        # shells that export CC/CXX. For bare profile (no CC in inherited env),
        # the conf assignment creates an unexported variable children cannot see.
        if cc_override is not None:
            extra_env["CC"] = cc_override
        if cxx_override is not None:
            extra_env["CXX"] = cxx_override
        if injected_env:
            extra_env.update(injected_env)

        if kernel_build:
            effective_cc = (
                cc_override
                or resolved_profile.get("CC")
                or os.environ.get("CC", "")
            )
            compiler_name = Path(effective_cc).name if effective_cc else ""
            if compiler_name.startswith("clang"):
                extra_env.update({"LLVM": "1", "LLVM_IAS": "1"})
                _kernel_log.info(f"Detected clang ({effective_cc!r}): injecting LLVM=1 LLVM_IAS=1")
            else:
                _kernel_log.info(f"Non-clang toolchain ({effective_cc!r} → 'gcc'): GCC kernel build")

        # Outer loop: on ToolchainMismatchError, regenerate the makepkg.conf
        # once with reactive_gcc_fallback=True (forcing the GCC+LTO guard on)
        # and retry the build. A second mismatch falls through to normal
        # failure handling.
        _reactive_retry_used = False
        while True:
            try:
                with emit_makepkg_conf(
                        resolved_profile, active_consumes,
                        cc_override=cc_override,
                        cxx_override=cxx_override,
                        ld_override=ld_override,
                        kernel_build=kernel_build,
                        compiler_flags_extra=compiler_flags_extra,
                        linker_flags_extra=linker_flags_extra,
                        strip_full_lto=strip_full_lto,
                        pkgbuild_has_hardcoded_gcc=pkgbuild_has_hardcoded_gcc,
                        reactive_gcc_fallback=_reactive_retry_used) as conf_path:
                    _invoke_with_retry(
                        pkgbuild_path, conf_path, resolved_profile,
                        extra_env, extra_flags, interactive, strip_flags)
                break
            except ToolchainMismatchError as e:
                if _reactive_retry_used:
                    _flag_log.error(
                        "Toolchain mismatch persists after auto-retry — "
                        "aborting (check the PKGBUILD and profile flags)"
                    )
                    raise RuntimeError(f"[build_failed] {e}")
                _flag_log.warn(
                    "Auto-retrying build with GCC-compatible flags "
                    "(rewriting clang-only flags like -flto=thin)"
                )
                _reactive_retry_used = True

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
                _patch_log.warn(f"Build failed — leaving {artifacts} in place for diagnosis")
        else:
            if success:
                if pkgbuild_path.exists():
                    pkgbuild_path.unlink()
                    _build_log.info(f"Removed patched PKGBUILD: {pkgbuild_path}")
            else:
                _build_log.warn(f"Build failed — leaving patched PKGBUILD in place: {pkgbuild_path}")


# ---------------------------------------------------------------------------
# PGO profdata helpers
# ---------------------------------------------------------------------------

_DEFAULT_PGO_STORE = "/var/tmp/sysforge-llvm-pgo"


def _try_load_toml(path: Path) -> dict | None:
    """Load a TOML file, returning None on any error."""
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError, KeyError, ValueError):
        return None


class PGOBuildSkipped(Exception):
    """
    Raised by run() when build_mode is pgo_llvm_toolchain but profdata is
    absent or version-incompatible and the user chose to skip (or input is
    non-interactive).  Callers (e.g. update.py) should treat this as a
    deliberate skip rather than a build failure.
    """


def _resolve_pgo_state(pkgbuild_path: Path) -> tuple[str, str]:
    """
    Check whether a saved clang.profdata is present and compatible with the
    PKGBUILD being built.

    Returns one of:
      ("ready",    str(profdata_path))  — profdata exists, major version matches
      ("mismatch", reason_str)          — profdata exists but major version differs
      ("absent",   reason_str)          — profdata or sidecar missing / toolchain.toml absent
    """
    toolchain_path = TOOLCHAIN_PATH
    if not toolchain_path.exists():
        return ("absent", "toolchain.toml not found — no pgo_store configured")
    try:
        with open(toolchain_path, "rb") as f:
            tcfg = tomllib.load(f)
    except Exception as e:
        return ("absent", f"cannot read toolchain.toml: {e}")

    pgo_store = Path(tcfg.get("pgo_store", _DEFAULT_PGO_STORE))
    profdata_path = pgo_store / "clang.profdata"
    version_path = pgo_store / "clang.profdata.version"

    if not profdata_path.exists():
        return ("absent", f"no profdata at {profdata_path}")
    if not version_path.exists():
        return ("absent", f"profdata version sidecar missing at {version_path}")

    saved_major = version_path.read_text().strip()

    # Extract the target LLVM major version from the PKGBUILD's pkgver line.
    try:
        content = pkgbuild_path.read_text()
        m = re.search(r"^pkgver=([^\s\n]+)", content, re.MULTILINE)
        if not m:
            return ("absent", "cannot determine pkgver from PKGBUILD")
        target_major = m.group(1).split(".")[0]
    except OSError as e:
        return ("absent", f"cannot read PKGBUILD: {e}")

    if saved_major != target_major:
        return (
            "mismatch",
            f"profdata is from LLVM {saved_major}, building LLVM {target_major}",
        )

    return ("ready", str(profdata_path))


# ---------------------------------------------------------------------------
# Build options
# ---------------------------------------------------------------------------

@dataclass
class BuildOptions:
    """
    Options for a single makepkg_wrapper.run() invocation.

    All fields default to their safe/do-nothing values so call sites
    only need to specify what they actually care about. Adding a new
    option only requires: add a field here with its default, handle it
    in run() — no changes needed at call sites that don't use it.
    """
    extra_flags: list | None = None
    interactive: bool = False
    pkg_log: bool = True
    persist_log: bool = False
    log_dir: Path | None = None
    profile_conf: str | None = None
    cc_override: str | None = None
    cxx_override: str | None = None
    ld_override: str | None = None
    cache_report: bool = False
    init_session: bool = True
    update: bool = True
    profile_override: str | None = None
    compiler_flags_extra: str | None = None
    linker_flags_extra: str | None = None
    strip_full_lto: bool = False
    extra_env: dict | None = None
    state_dir: Path | None = None
    abi_check: bool = False
    strip_flags: frozenset | set | None = None
    force_batch: bool = False
    pgo_managed: bool = False


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(pkgbuild_path, options: BuildOptions | None = None):
    if options is None:
        options = BuildOptions()

    config_paths = [Path(options.profile_conf)] if options.profile_conf is not None else None
    config = load_config(config_paths=config_paths)
    # Note: load_config() debug dumps (full flag_profiles etc.) fire here, before the
    # pkg log is open — they go to the unified log (if open) and terminal only.

    try:
        pkgbuild_path = find_pkgbuild(str(pkgbuild_path), config)
    except FileNotFoundError as e:
        _build_log.fatal(str(e))

    # Open per-package log as early as possible so subsequent debug output is captured.
    # Use the PKGBUILD directory name; this matches pkgbase in all normal cases.
    if options.pkg_log:
        log_base = Path(options.log_dir) if options.log_dir is not None else pkgbuild_path.parent
        log_path = log_base / f"sysforge_{pkgbuild_path.parent.name}.log"
        log.open_pkg_log(log_path, argv=sys.argv)
        _build_log.info(f"Per-package log: {log_path}")

    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    if options.init_session:
        reset_session()
        emit_system_probes()

    if options.update:
        try:
            git_pull_rebase(pkgbuild_path.parent)
        except RuntimeError as e:
            _git_log.fatal(str(e))
    else:
        _build_log.info("--no-update: skipping git pull --rebase")

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        handle_failure("pkgbuild_unparseable", str(e), config)
        pkgmeta = {"globals": {}}

    pkgbuild_has_hardcoded_gcc = has_hardcoded_gcc(pkgmeta)
    if pkgbuild_has_hardcoded_gcc:
        _flag_log.info(
            "PKGBUILD build()/package() invokes hardcoded gcc/g++ — "
            "GCC flag guard will be applied even if the active profile sets CC=clang"
        )

    build_success = False
    try:
        matched_rules = match_rules(pkgmeta, config.get("rules", []))

        if options.profile_override is not None:
            # Bypass rule matching: resolve the named profile directly.
            from sysforge.primitives.profile import merge_extends
            profiles = config.get("profiles", {})
            if options.profile_override not in profiles:
                raise RuntimeError(
                    f"[BUILD] profile_override {options.profile_override!r} not found in loaded config"
                )
            resolved_profile = merge_extends(options.profile_override, profiles, conflict_groups=conflict_groups)
            build_mode = resolved_profile.get("build_mode")
            _build_log.info(f"Profile override: {options.profile_override!r} (build_mode={build_mode!r})")
        else:
            build_mode = get_build_mode(matched_rules, config)
            resolved_profile = None  # resolved below after extracted_profile is known

        kernel_build = (build_mode == "kernel")

        # pgo_llvm_toolchain: inject -fprofile-use if saved profdata is compatible,
        # otherwise prompt to plain-build or skip (default: skip).
        # Skip this block when pgo_managed=True — the toolchain stage controls PGO
        # flags directly via compiler_flags_extra; we must not override or prompt.
        effective_flags_extra = options.compiler_flags_extra
        if build_mode == "pgo_llvm_toolchain" and not options.pgo_managed:
            pgo_state, pgo_info = _resolve_pgo_state(pkgbuild_path)
            if pgo_state == "ready":
                pgo_flag = f"-fprofile-use={pgo_info}"
                effective_flags_extra = (
                    f"{options.compiler_flags_extra} {pgo_flag}".strip()
                    if options.compiler_flags_extra
                    else pgo_flag
                )
                _pgo_log.warn(
                    "PGO-toolchain build path is experimental and deferred to post-1.0",
                )
                _pgo_log.info(f"Reusing profdata for PGO build: {pgo_info}")
            else:
                reason = pgo_info
                if sys.stdin.isatty():
                    from sysforge.ui import progress as _ui_progress
                    _ui_progress.clear()
                    try:
                        choice = input(
                            _pgo_log.prompt_prefix("WARN")
                            + f"PGO profdata unavailable ({reason})."
                            + " [p]lain build or [s]kip? [S]: "
                        ).strip().lower()
                    except EOFError:
                        choice = ""
                else:
                    choice = ""
                    _pgo_log.warn(
                        f"Non-interactive: skipping pgo_llvm_toolchain build ({reason})",
                    )
                if choice not in ("p", "plain"):
                    raise PGOBuildSkipped(
                        f"[PGO] Skipped {pkgbuild_path.parent.name!r}: {reason}. "
                        "Run 'sysforge run toolchain' to regenerate profdata."
                    )
                _pgo_log.warn(f"Building without PGO: {reason}")

        extracted_profile = None
        if build_mode in ("patched_pkgbuild", "kernel"):
            extracted_profile = extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
            if extracted_profile:
                write_extracted_profile(extracted_profile, pkgbuild_path)

        if options.profile_override is None:
            resolved_profile = resolve_profile(
                pkgmeta, matched_rules, config, conflict_groups,
                extracted_profile=extracted_profile,
            )
        if options.force_batch and not resolved_profile.get("batch", False):
            resolved_profile = dict(resolved_profile)
            resolved_profile["batch"] = True
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
            _build_log.info(f"Cleaned build dir: {build_dir}")

        import_pgp_keys(pkgmeta, pkgbuild_path)
        run_dep_analysis(pkgmeta, config)

        import time as _time
        _build_start = _time.time()
        _run_build(
                pkgbuild_path, resolved_profile, config, groups,
                active_consumes=active_consumes,
                extracted_profile=extracted_profile if build_mode in ("patched_pkgbuild", "kernel") else None,
                pkgmeta=pkgmeta,
                extra_flags=options.extra_flags,
                interactive=options.interactive,
                cc_override=options.cc_override,
                cxx_override=options.cxx_override,
                ld_override=options.ld_override,
                kernel_build=kernel_build,
                compiler_flags_extra=effective_flags_extra,
                linker_flags_extra=options.linker_flags_extra,
                strip_full_lto=options.strip_full_lto,
                injected_env=options.extra_env,
                strip_flags=options.strip_flags,
                pkgbuild_has_hardcoded_gcc=pkgbuild_has_hardcoded_gcc,
            )
        build_success = True

        # Post-build: abort if profraw files are accumulating outside PGO pass 2.
        # This catches instrumented LLVM binaries leaking onto the system after a
        # failed or partial toolchain run — the builds silently emit profraw that
        # can fill the disk.  Fatal because continued builds would keep generating
        # profraw until storage is exhausted.
        #
        # Files predating _build_start are orphans from a prior run the user has
        # since cleaned up (e.g. reinstalled llvm/llvm-libs); purge them rather
        # than aborting forever.  Only profraw files touched by THIS build signal
        # a still-instrumented system.
        if not options.pgo_managed:
            _tcfg = _try_load_toml(TOOLCHAIN_PATH) if TOOLCHAIN_PATH.exists() else None
            _pgo_store = Path(
                _tcfg.get("pgo_store", _DEFAULT_PGO_STORE)
                if _tcfg is not None
                else _DEFAULT_PGO_STORE
            )
            if _pgo_store.is_dir():
                _all_profraw = list(_pgo_store.glob("**/*.profraw"))
                # 1s slack absorbs filesystem mtime rounding on second-granularity fs.
                _freshness_cutoff = _build_start - 1
                _fresh_profraw = [
                    f for f in _all_profraw if f.stat().st_mtime >= _freshness_cutoff
                ]
                _orphan_profraw = [
                    f for f in _all_profraw if f.stat().st_mtime < _freshness_cutoff
                ]
                if _fresh_profraw:
                    _total_bytes = sum(p.stat().st_size for p in _fresh_profraw)
                    _pgo_log.fatal(
                        f"{len(_fresh_profraw)} stale .profraw files "
                        f"({_total_bytes / 1024 / 1024:.1f} MiB) in {_pgo_store} — "
                        "instrumented LLVM binaries may be installed on this system. "
                        "Reinstall clean llvm/llvm-libs or run 'sysforge run toolchain' "
                        "to complete the PGO build."
                    )
                if _orphan_profraw:
                    _orphan_bytes = sum(p.stat().st_size for p in _orphan_profraw)
                    for _f in _orphan_profraw:
                        try:
                            _f.unlink()
                        except OSError:
                            pass
                    _pgo_log.info(
                        f"Purged {len(_orphan_profraw)} orphaned .profraw file(s) "
                        f"({_orphan_bytes / 1024 / 1024:.1f} MiB) from {_pgo_store} "
                        "(prior run residue; current build produced none)"
                    )

        # Post-build ABI check (non-fatal)
        if options.abi_check:
            try:
                from sysforge.primitives.abi_check import check_package_abi
                built_pkgs = _find_built_packages(pkgbuild_path.resolve().parent)
                if not built_pkgs:
                    _abi_log.info("No built packages found for ABI check")
                for pkg in built_pkgs:
                    issues = check_package_abi(pkg)
                    if issues:
                        for issue in issues:
                            _abi_log.warn(issue)
                    else:
                        _abi_log.info(f"{pkg.name}: OK")
            except Exception as e:
                _abi_log.warn(f"ABI check failed: {e}")

        # Record build metadata for `sysforge update` (non-fatal)
        try:
            from sysforge.primitives.build_state import BuildState
            from sysforge.pipeline.state import resolve_state_dir
            _state_dir, _ = resolve_state_dir(options.state_dir)
            bs = BuildState(_state_dir)
            globals_ = pkgmeta.get("globals", {})
            pkgnames = globals_.get("pkgname", [])
            if isinstance(pkgnames, str):
                pkgnames = [pkgnames]
            pkgbase = globals_.get("pkgbase") or (pkgnames[0] if pkgnames else "unknown")
            fs = serialize_flags(resolved_profile) if resolved_profile is not None else None

            # Prefer pkgver/pkgrel/epoch from the built .pkg.tar.* filenames
            # over the static PKGBUILD parse. The parser intentionally leaves
            # shell parameter-expansion forms (e.g. ``${_ver/[a-z]/.${_ver//[0-9.]/}}``)
            # untouched, so packages using them would otherwise record a
            # literal ``$...`` string as pkgver and always mismatch vercmp.
            filename_versions: dict[str, tuple[str, str, str]] = {}
            for p in _find_built_packages(pkgbuild_path.resolve().parent):
                for name in pkgnames:
                    if name in filename_versions:
                        continue
                    parsed = _parse_built_pkg_filename(name, p.name)
                    if parsed is not None:
                        filename_versions[name] = parsed

            for name in pkgnames:
                ep, ver, rel = filename_versions.get(name, (
                    globals_.get("epoch", "0"),
                    globals_.get("pkgver", ""),
                    globals_.get("pkgrel", "1"),
                ))
                bs.record(
                    pkgname=name,
                    pkgver=ver,
                    pkgrel=rel,
                    epoch=ep,
                    pkgbase=pkgbase,
                    pkgbuild_dir=pkgbuild_path.parent,
                    build_mode="profiled",
                    flags_string=fs,
                )
            bs.save()
            _build_log.info(f"Recorded build state for {pkgbase!r}")
        except Exception as e:
            _build_log.warn(f"Failed to record build state: {e}")

    finally:
        if options.pkg_log:
            log.close_pkg_log(success=build_success, persist=options.persist_log)

    if options.cache_report:
        emit_session_report()


if __name__ == "__main__":
    run(sys.argv[1])
