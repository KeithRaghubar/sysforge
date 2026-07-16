# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_conf.py — temp makepkg.conf emission

Builds a complete, self-contained temp ``makepkg.conf`` by merging the system
``/etc/makepkg.conf`` baseline with sysforge's profile-managed keys and CLI
toolchain overrides, then applying the linker guard, the GCC+LTO guard, the
lib32 ``-march`` scrub, and PGO/kernel flag handling.  Yielded as a context
manager that writes the temp conf on entry and removes it on exit.  Owns the
``[CONF]`` tag.

All conf-assembly narration — including the linker guard, the GCC+LTO guard,
the lib32 ``-march`` scrub, and PGO/kernel flag adjustments — is emitted under
``[CONF]``: these are decisions *this* module makes while assembling a correct
conf.  The flag-string transforms themselves stay pure in ``makepkg_flags``
(called here as ``(cleaned, stripped)`` returns), so ``makepkg_flags`` keeps
``[FLAG]`` for its own generic flag work and this module owns a single tag.
Re-exported from ``makepkg_wrapper`` as ``emit_makepkg_conf``.
"""
import contextlib
import shutil
import tempfile
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_throttle import apply_jobs_to_makeflags
from sysforge.primitives.config import parse_system_makepkg_conf
from sysforge.primitives.pkgbuild_meta import options_list_disabled
from sysforge.primitives.makepkg_flags import (
    _detect_linker_from_rustflags,
    _detect_linker_from_ldflags,
    _inject_linker,
    _replace_rustflags_linker,
    _scrub_lib32_arch_flags,
    _strip_all_lto,
    _strip_full_lto,
    _strip_lld_flags,
    _strip_pgo_flags,
    resolve_effective_linker,
)
from sysforge.primitives.profile import CONF_KEY_MAP, KERNEL_CLEAN_KEYS, SYSFORGE_KEYS

_conf_log = log.get_logger("CONF")


@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile, active_consumes=None,
                      system_conf_path=None,
                      cc_override=None, cxx_override=None, ld_override=None,
                      kernel_build: bool = False,
                      compiler_flags_extra: str | None = None,
                      linker_flags_extra: str | None = None,
                      strip_full_lto: bool = False,
                      pkgbuild_has_hardcoded_gcc: bool = False,
                      reactive_gcc_fallback: bool = False,
                      is_lib32: bool = False,
                      is_musl_static: bool = False,
                      pkgbuild_options: list | None = None,
                      toolchain_variant: str | None = None,
                      jobs: int | None = None):
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

    is_lib32: when True, strip 64-bit-only ``-march=`` tokens from
    CFLAGS/CXXFLAGS in both system-conf passthrough and profile overrides.
    ``-march=native`` resolves to the host's amd64 microarch and ``-march=
    x86-64[-v2|v3|v4]`` are 64-bit ISA levels — both are rejected by
    multilib GCC when building i686 objects. Set by callers that detect
    lib32-* from the PKGBUILD directory name.

    is_musl_static: when True (a static-musl bootstrap like pacman-static,
    detected via ``pkgbuild_meta.is_musl_static_build``), force the bfd linker
    and scrub PGO flags from CFLAGS/CXXFLAGS/LDFLAGS in both profile overrides
    and system-conf passthrough. ``-fuse-ld=lld`` + ``-static`` + musl yields a
    startup-crashing binary, and musl-gcc cannot consume a clang ``.profdata``.
    The musl analogue of ``is_lib32``; reuses the same strip helpers.
    """
    env_keys = CONF_KEY_MAP.get("env", set())
    # Toolchain keys (CC, CXX) are delivered via subprocess env, not via the
    # conf file. makepkg sources the conf as a shell script, so any CC/CXX
    # present in the system conf would overwrite the env-injected values.
    # Exclude them from conf output alongside env-type keys.
    toolchain_keys = CONF_KEY_MAP.get("toolchain", set())
    conf_exclude_keys = env_keys | toolchain_keys

    allowed_keys: set[str] | None
    if active_consumes is None:
        allowed_keys = None
    else:
        allowed_keys = set()
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
        _conf_log.info("Kernel build: omitting profile flag keys from makepkg.conf "
                  "(CFLAGS/CXXFLAGS/LDFLAGS/CPPFLAGS/DEBUG_*); system conf values preserved")

    # Load system conf baseline
    system_assignments = parse_system_makepkg_conf(system_conf_path)

    # Build-throttle job cap: force the -j token in MAKEFLAGS. The effective
    # base is the profile MAKEFLAGS if set, else the system conf value (unquoted),
    # else empty; the result is written via profile_overrides so it wins. Folding
    # it in here (rather than in the profile) keeps parallelism control in one
    # home with the nice/ionice/cpu_quota knobs (see build_throttle).
    if jobs is not None:
        base_makeflags = profile_overrides.get("MAKEFLAGS")
        if base_makeflags is None and "MAKEFLAGS" in system_assignments:
            _raw = system_assignments["MAKEFLAGS"].strip()
            base_makeflags = _raw[1:-1] if (len(_raw) >= 2 and _raw[0] == _raw[-1] == '"') else _raw
        base_makeflags = base_makeflags or ""
        profile_overrides["MAKEFLAGS"] = apply_jobs_to_makeflags(base_makeflags, jobs)
        _conf_log.info(f"Build-throttle: capped MAKEFLAGS jobs to -j{jobs}")

    # Apply CLI toolchain overrides on top of profile values
    if cc_override is not None:
        profile_overrides["CC"] = cc_override
        _conf_log.info(f"CC overridden via --cc: {cc_override}")
    if cxx_override is not None:
        profile_overrides["CXX"] = cxx_override
        _conf_log.info(f"CXX overridden via --cxx: {cxx_override}")
    if ld_override is not None:
        if kernel_build:
            _conf_log.info(f"--ld={ld_override!r} ignored for kernel build (use LLVM=1 for lld)")
        else:
            current_ldflags = profile_overrides.get("LDFLAGS", "")
            profile_overrides["LDFLAGS"] = _inject_linker(current_ldflags, ld_override)
    elif (
        not kernel_build
        and toolchain_variant in ("stock_llvm", "pgo_llvm")
    ):
        _sd_ldflags = profile_overrides.get("LDFLAGS")
        if _sd_ldflags is None and "LDFLAGS" in system_assignments:
            _raw = system_assignments["LDFLAGS"].strip()
            _sd_ldflags = _raw[1:-1] if (len(_raw) >= 2 and _raw[0] == _raw[-1] == '"') else _raw
        _sd_ldflags = _sd_ldflags or ""
        if not _detect_linker_from_ldflags(_sd_ldflags) and shutil.which("lld"):
            profile_overrides["LDFLAGS"] = _inject_linker(_sd_ldflags, "lld")
            _conf_log.info(
                f"[VARIANT_LD] variant={toolchain_variant!r}: defaulting -fuse-ld=lld "
                "(no linker declared in LDFLAGS, lld available)"
            )

    # Linker guard: determine the effective linker. If no -fuse-ld=X is declared,
    # the effective linker is the system default (bfd). Strip lld-specific flags
    # whenever the effective linker is not lld — not only when a linker is declared
    # but missing, since undeclared LDFLAGS containing lld-only flags will break
    # configure test compilations against the system linker.
    #
    # Detection sources LDFLAGS from profile_overrides first, then falls back to
    # the system conf — minimal profiles (like ``bare``) often do not override
    # LDFLAGS, so the system conf's LDFLAGS (e.g. ``-fuse-ld=lld``) is what
    # actually reaches makepkg.
    # Effective linker: one definition, shared with the patch layer.
    # _ldflags_source tells the lld-flag-strip block below whether the value we
    # could rewrite is the profile's (writable) or the system conf's (read-only).
    _ldflags_source: str | None = None  # "profile" | "system" | None
    _profile_ldflags = profile_overrides.get("LDFLAGS")
    _system_ldflags = ""
    if "LDFLAGS" in system_assignments:
        _raw = system_assignments["LDFLAGS"].strip()
        _system_ldflags = _raw[1:-1] if (len(_raw) >= 2 and _raw[0] == _raw[-1] == '"') else _raw
    if _profile_ldflags is not None:
        _ldflags_source = "profile"
    elif "LDFLAGS" in system_assignments:
        _ldflags_source = "system"
    effective_linker = resolve_effective_linker(
        ld_override=None,
        profile_ldflags=_profile_ldflags,
        system_ldflags="" if _ldflags_source == "profile" else _system_ldflags,
    )
    if _ldflags_source is not None:
        declared_linker = _detect_linker_from_ldflags(
            _profile_ldflags if _ldflags_source == "profile" else _system_ldflags
        )
        if declared_linker and not shutil.which(declared_linker):
            _conf_log.warn(
                f"Declared linker '{declared_linker}' not found on PATH — "
                "treating effective linker as 'ld'"
            )

        # Only strip lld-only flags from LDFLAGS we own (profile_overrides). The
        # system conf is read-only here; if its LDFLAGS contains lld-only flags
        # and the effective linker isn't lld, the GCC+lld disable branch below
        # (or the explicit override branch) is what writes a corrected value.
        if effective_linker != "lld" and _ldflags_source == "profile":
            cleaned, stripped_tokens = _strip_lld_flags(profile_overrides["LDFLAGS"])
            if stripped_tokens:
                _conf_log.warn(
                    f"Effective linker is '{effective_linker}' (not lld) — "
                    "stripping lld-specific flags from LDFLAGS"
                )
                for tok in stripped_tokens:
                    _conf_log.warn(f"Stripped lld-only flag: {tok}")
                profile_overrides["LDFLAGS"] = cleaned

        # RUSTFLAGS linker reconciliation: if RUSTFLAGS declares a different
        # linker than LDFLAGS, override it to match. A mismatch causes link
        # failures when LTO is enabled — mold cannot resolve LLVM bitcode
        # produced by lld, and vice versa.
        if "RUSTFLAGS" in profile_overrides:
            rust_linker = _detect_linker_from_rustflags(profile_overrides["RUSTFLAGS"])
            if rust_linker and rust_linker != effective_linker:
                _conf_log.warn(
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
            _conf_log.warn(
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
        _conf_log.warn(
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
                    _conf_log.warn(
                              f"Stripped full-LTO flag(s) from {key} (incompatible with IR PGO): "
                              f"{' '.join(stripped_toks)}")
                    profile_overrides[key] = cleaned
        # Clear LTOFLAGS so makepkg's lto option doesn't re-inject -flto=thin at
        # build time (LTOFLAGS is appended to CFLAGS/CXXFLAGS/LDFLAGS by makepkg
        # when OPTIONS contains lto, bypassing the stripping above).
        profile_overrides["LTOFLAGS"] = ""
        _conf_log.info("Cleared LTOFLAGS for PGO pass (LTO disabled)")

    # PKGBUILD options=('!lto') scrub. The author declared LTO breaks this
    # package (the cosmic-edit/onig mold-failure class). makepkg's own !lto only
    # suppresses *its* LTOFLAGS injection — profile-baked -flto in CFLAGS/
    # CXXFLAGS/LDFLAGS still reaches the compiler — so strip those here and clear
    # LTOFLAGS. Resolve each key's effective value (profile override else
    # unquoted system conf) and write it back as an owned override, so the
    # emission loop never re-emits a raw system value carrying -flto (F9).
    if options_list_disabled(pkgbuild_options, "lto"):
        _lto_stripped = False
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                val = profile_overrides[key]
            elif key in system_assignments:
                raw = system_assignments[key].strip()
                val = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
            else:
                continue
            cleaned, stripped = _strip_all_lto(val)
            if stripped:
                profile_overrides[key] = cleaned
                _lto_stripped = True
        # Clear LTOFLAGS so makepkg's lto hook can't re-inject via ${LTOFLAGS:--flto}.
        if "LTOFLAGS" in profile_overrides or "LTOFLAGS" in system_assignments:
            profile_overrides["LTOFLAGS"] = ""
        if _lto_stripped:
            _conf_log.info("PKGBUILD options=('!lto') — stripped -flto flag(s) "
                           "and cleared LTOFLAGS (author declared LTO unsupported)")

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
        _conf_log.info(f"Injecting into CFLAGS/CXXFLAGS/LDFLAGS: {compiler_flags_extra!r}")

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
        _conf_log.info(f"Injecting into LDFLAGS: {linker_flags_extra!r}")

    # lib32 guards (profile overrides): strip 64-bit-only -march tokens from
    # CFLAGS/CXXFLAGS, and lld --icf=* tokens from LDFLAGS. The icf scrub is
    # unconditional on the linker — 32-bit ICF (identical-code-folding) breaks
    # links for some lib32 packages (e.g. lib32-lzo) even when lld is active,
    # so unlike the linker-gated lld-flag strip above we always drop it here.
    # The system-conf passthrough for both is scrubbed at the emission loop
    # below so we never write a known-broken value either way.
    if is_lib32:
        for key in ("CFLAGS", "CXXFLAGS"):
            if key in profile_overrides:
                cleaned, stripped_tokens = _scrub_lib32_arch_flags(profile_overrides[key])
                if stripped_tokens:
                    _conf_log.info(
                        f"lib32 build: stripped 64-bit-only -march tokens from "
                        f"profile {key}: {stripped_tokens}"
                    )
                    profile_overrides[key] = cleaned
        if "LDFLAGS" in profile_overrides:
            cleaned, stripped_tokens = _strip_lld_flags(profile_overrides["LDFLAGS"])
            if stripped_tokens:
                _conf_log.info(
                    f"lib32 build: stripped lld --icf flag(s) from profile "
                    f"LDFLAGS: {stripped_tokens}"
                )
                profile_overrides["LDFLAGS"] = cleaned
        # Strip PGO profile flags: the toolchain stage injects
        # -fprofile-use=<store>/clang.profdata into CFLAGS/CXXFLAGS/LDFLAGS via
        # compiler_flags_extra (above), but the profile is trained on the x86_64
        # clang self-build and is meaningless for an i686 (-m32) build — clang
        # discards it (-Wbackend-plugin "count discarded"). This runs after the
        # injection so it catches the injected flag regardless of source.
        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            if key in profile_overrides:
                cleaned, stripped_tokens = _strip_pgo_flags(profile_overrides[key])
                if stripped_tokens:
                    _conf_log.info(
                        f"lib32 build: stripped PGO profile flag(s) from "
                        f"profile {key}: {stripped_tokens}"
                    )
                    profile_overrides[key] = cleaned

    # musl-static guards (pacman-static et al.): force the bfd linker and scrub
    # PGO flags. -fuse-ld=lld + -static + musl produces a startup-crashing binary
    # (the conftest segfaults at configure time), and musl-gcc cannot consume a
    # clang .profdata. The failing flags usually live in the system conf
    # (/etc/makepkg-clang.conf carries -fuse-ld=lld), so for each key we resolve
    # the effective value (profile override else unquoted system conf), scrub it,
    # and write it back as an *owned* override — so the emission loop emits the
    # scrubbed value once and never re-emits the raw system value.
    if is_musl_static:
        def _effective(key):
            if key in profile_overrides:
                return profile_overrides[key]
            if key in system_assignments:
                raw = system_assignments[key].strip()
                return raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
            return None

        ldflags = _effective("LDFLAGS")
        if ldflags is not None:
            changed = False
            if _detect_linker_from_ldflags(ldflags) == "lld":
                ldflags = _inject_linker(ldflags, "bfd")
                changed = True
                _conf_log.info("musl-static build: forced bfd linker "
                               "(-fuse-ld=lld + -static + musl crashes at runtime)")
            ldflags, stripped = _strip_lld_flags(ldflags)
            if stripped:
                changed = True
                _conf_log.info(f"musl-static build: stripped lld-only flag(s) from "
                               f"LDFLAGS: {stripped}")
            # Only claim ownership when we actually scrubbed something; a
            # pristine LDFLAGS stays in system-conf passthrough untouched.
            if changed:
                profile_overrides["LDFLAGS"] = ldflags

        for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
            val = _effective(key)
            if val is None:
                continue
            cleaned, stripped = _strip_pgo_flags(val)
            if stripped:
                _conf_log.info(f"musl-static build: stripped PGO profile flag(s) "
                               f"from {key}: {stripped}")
                profile_overrides[key] = cleaned

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
            if is_lib32 and key in ("CFLAGS", "CXXFLAGS"):
                raw = raw_val.strip()
                inner = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
                cleaned, stripped_tokens = _scrub_lib32_arch_flags(inner)
                if stripped_tokens:
                    _conf_log.info(
                        f"lib32 build: stripped 64-bit-only -march tokens from "
                        f"system {key}: {stripped_tokens}"
                    )
                    conf_lines.append(f'{key}="{cleaned}"')
                    continue
            if is_lib32 and key == "LDFLAGS":
                raw = raw_val.strip()
                inner = raw[1:-1] if (len(raw) >= 2 and raw[0] == raw[-1] == '"') else raw
                cleaned, stripped_tokens = _strip_lld_flags(inner)
                if stripped_tokens:
                    _conf_log.info(
                        f"lib32 build: stripped lld --icf flag(s) from "
                        f"system {key}: {stripped_tokens}"
                    )
                    conf_lines.append(f'{key}="{cleaned}"')
                    continue
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
        encoding="utf-8", mode="w",
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
        Path(tmp_path).unlink()
        _conf_log.info(f"Removed temp makepkg.conf: {tmp_path}")
