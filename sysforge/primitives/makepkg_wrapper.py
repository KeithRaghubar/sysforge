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

from sysforge.primitives.config import (
    load_config,
    load_conflict_groups,
    load_consumes_inference,
)
from sysforge.primitives.pkgbuild_meta import parse_pkgbuild
from sysforge.primitives.pkgbuild_patcher import (
    apply_patch_pkgbuild,
    cleanup_patch_artifacts,
    extract_pkgbuild_profile,
    patch_pkgbuild_groups,
    write_extracted_profile,
)
from sysforge.primitives.dep_analysis import run_dep_analysis
from sysforge.primitives.failure import handle_failure
from sysforge.primitives.profile import (
    _CONF_KEY_MAP,
    _SYSFORGE_KEYS,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# Conf file emission
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile, active_consumes=None):
    """
    Write conf-destined keys from resolved_profile to a temp makepkg.conf.
    Yields the path to the temp file for use with MAKEPKG_CONF.
    Cleans up the temp file on exit.

    Keys written: those belonging to any non-"env" conf type present in
    active_consumes. If active_consumes is None, writes all non-internal,
    non-"env" keys (backward-compatible fallback).

    "env" type keys and unknown keys are delivered separately via
    resolve_env_vars() and injected on the makepkg subprocess invocation.
    """
    # Build the set of all "env"-type keys so we can exclude them here
    env_keys = _CONF_KEY_MAP.get("env", set())

    if active_consumes is None:
        # Fallback: write everything that isn't sysforge-internal or env-pass
        allowed_keys = None
    else:
        allowed_keys: set[str] = set()
        for conf_type in active_consumes:
            if conf_type == "env":
                continue  # env keys travel separately
            if conf_type in _CONF_KEY_MAP:
                allowed_keys.update(_CONF_KEY_MAP[conf_type])

    conf_lines = []
    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue
        if key in env_keys:
            continue  # delivered via env pass
        if allowed_keys is not None and key not in allowed_keys:
            continue  # unknown key — delivered via env pass
        conf_lines.append(f'{key}="{val}"')

    with tempfile.NamedTemporaryFile(
        mode="w",
        prefix="sysforge_makepkg_",
        suffix=".conf",
        delete=False,
    ) as f:
        f.write("\n".join(conf_lines) + "\n")
        tmp_path = f.name

    print(f"[CONF] Wrote temp makepkg.conf: {tmp_path}")
    try:
        yield tmp_path
    finally:
        os.unlink(tmp_path)
        print(f"[CONF] Removed temp makepkg.conf: {tmp_path}")


# ---------------------------------------------------------------------------
# Env var resolution and build invocation
# ---------------------------------------------------------------------------

def resolve_env_vars(resolved_profile, active_consumes=None):
    """
    Extract profile keys that travel via subprocess env injection rather than
    the makepkg.conf temp file.

    Two categories are collected:
      1. Keys in the "env" conf type — explicitly designated for env injection
         (e.g. RUSTC_WRAPPER, CCACHE_DIR). Only collected when "env" is in
         active_consumes or active_consumes is None (fallback mode).
      2. Unknown keys — not in any _CONF_KEY_MAP type and not in _SYSFORGE_KEYS.
         These are always collected and logged under [ENV] as a warning, since
         their presence may indicate a typo or a new key that needs classifying.

    Returns dict[str, str] of key -> value pairs to inject on invocation.
    Empty dict if nothing to inject.
    """
    env_type_keys = _CONF_KEY_MAP.get("env", set())

    # All keys that are explicitly classified into a non-env conf type
    all_conf_keys: set[str] = set()
    for conf_type, keys in _CONF_KEY_MAP.items():
        if conf_type != "env":
            all_conf_keys.update(keys)

    collect_env_type = active_consumes is None or "env" in active_consumes

    result: dict[str, str] = {}
    unknown: list[str] = []

    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue

        if key in env_type_keys:
            if collect_env_type:
                result[key] = val
                print(f"[ENV] Injecting (env type): {key}={val!r}")
            continue

        if key not in all_conf_keys:
            # Unknown key — not classified; env pass with warning
            result[key] = val
            unknown.append(key)

    if unknown:
        print(
            f"[ENV] Unclassified profile keys injected via env (consider adding to "
            f"_CONF_KEY_MAP): {sorted(unknown)}"
        )

    return result


def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile, extra_env=None):
    pkgbuild_path = Path(pkgbuild_path).resolve()
    build_dir = pkgbuild_path.parent

    env = os.environ.copy()
    env["MAKEPKG_CONF"] = str(conf_path)

    if extra_env:
        env.update(extra_env)
        print(f"[ENV] Injecting {len(extra_env)} env var(s): {sorted(extra_env.keys())}")

    flags = resolved_profile.get("makepkg_flags", [])
    cmd = ["makepkg"] + flags

    print(
        f"[BUILD] Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}"
    )

    result = subprocess.run(cmd, cwd=build_dir, env=env)

    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "makepkg")


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile, extra_env=None):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.
    """
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile, extra_env)
        except subprocess.CalledProcessError as e:
            print(f"[BUILD] Build failed in batch mode, aborting: {e}")
            raise RuntimeError(f"[build_failed] {e}")
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile, extra_env)
                break
            except subprocess.CalledProcessError as e:
                print(f"[BUILD] Build failed: {e}")
                print(f"[BUILD] PKGBUILD location: {pkgbuild_path}")
                response = (
                    input(
                        "[BUILD] Manually correct the PKGBUILD and press Enter to retry, "
                        "or type 'abort' to stop: "
                    )
                    .strip()
                    .lower()
                )
                if response == "abort":
                    raise RuntimeError(
                        "[build_failed] Aborted by user after build failure"
                    )
                print("[BUILD] Retrying build...")


def _run_build(pkgbuild_path, resolved_profile, config, groups,
               active_consumes=None, extracted_profile=None, pkgmeta=None):
    """
    Emit makepkg.conf and invoke makepkg, handling build failures.

    If extracted_profile is provided (patch_pkgbuild mode), applies the
    patched PKGBUILD instead of the original. Cleans up patch artifacts on
    success; leaves them in place on failure for diagnosis.
    """
    if extracted_profile is not None:
        # patch_pkgbuild mode: use patched copy with flags stripped
        pkgbuild_path = apply_patch_pkgbuild(
            pkgbuild_path,
            pkgmeta or {"globals": {}},
            extracted_profile,
        )
    else:
        pkgbuild_path = patch_pkgbuild_groups(pkgbuild_path, groups)

    success = False
    try:
        extra_env = resolve_env_vars(resolved_profile, active_consumes)
        with emit_makepkg_conf(resolved_profile, active_consumes) as conf_path:
            _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile, extra_env)
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
                print(
                    f"[PATCH] Build failed — leaving PKGBUILD.sysforge and "
                    f"pkgbuild_extracted_profile.toml in place for diagnosis"
                )
        else:
            if pkgbuild_path.exists():
                pkgbuild_path.unlink()
                print(f"[BUILD] Removed patched PKGBUILD: {pkgbuild_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(pkgbuild_path):
    config = load_config()
    conflict_groups = load_conflict_groups()
    inference_map = load_consumes_inference()

    try:
        pkgmeta = parse_pkgbuild(pkgbuild_path)
    except Exception as e:
        handle_failure("pkgbuild_unparseable", str(e), config)
        pkgmeta = {"globals": {}}

    pkgbuild_path = Path(pkgbuild_path).resolve()
    matched_rules = match_rules(pkgmeta, config.get("rules", []))

    build_mode = None  # resolved below after profile

    # Determine if patching is requested before full profile resolution,
    # so we can extract and inject the pkgbuild_extracted root.
    # We do a preliminary rule match here; full profile resolution follows.
    _pre_profile = resolve_profile(pkgmeta, matched_rules, config, conflict_groups)
    build_mode = _pre_profile.get("build_mode")

    extracted_profile = None
    if build_mode == "patch_pkgbuild":
        extracted_profile = extract_pkgbuild_profile(pkgmeta, pkgbuild_path)
        if extracted_profile:
            write_extracted_profile(extracted_profile, pkgbuild_path)

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
        print(f"[BUILD] Cleaned build dir: {build_dir}")

    if build_mode == "pgo_llvm_toolchain":
        pass  # hand off to pgo handler
    else:
        run_dep_analysis(pkgmeta, config)
        _run_build(
            pkgbuild_path, resolved_profile, config, groups,
            active_consumes=active_consumes,
            extracted_profile=extracted_profile if build_mode == "patch_pkgbuild" else None,
            pkgmeta=pkgmeta,
        )


if __name__ == "__main__":
    run(sys.argv[1])
