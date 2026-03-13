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
from sysforge.primitives.profile import (
    _CONF_KEY_MAP,
    _SYSFORGE_KEYS,
    match_rules,
    resolve_consumes,
    resolve_groups,
    resolve_profile,
)


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------

_ALWAYS_ABORT = {"profile_missing", "tempfile_write_failed"}

_FAILURE_DEFAULTS = {
    "pkgbuild_unparseable": "warn_and_fallback",
    "no_rule_matched": "fallback",
    "profile_missing": "abort",
    "profile_cycle": "abort",
    "tempfile_write_failed": "abort",
    "env_conflict": "warn_and_fallback",
    "abi_mismatch": "warn_and_fallback",
    "dep_unsatisfied": "warn_and_fallback",
}

_VALID_BEHAVIOURS = {"abort", "warn_and_fallback", "fallback", "error"}


def handle_failure(scenario, message, config, fallback=None):
    """
    Handle a named failure scenario according to [failure_handling] config.

    Behaviours:
      abort             — log and raise RuntimeError immediately
      error             — log as error, raise RuntimeError
      warn_and_fallback — log as warning, return fallback value
      fallback          — return fallback value silently

    profile_missing and tempfile_write_failed always abort regardless of config.
    """
    failure_cfg = config.get("failure_handling", {})
    behaviour = failure_cfg.get(scenario, _FAILURE_DEFAULTS.get(scenario, "abort"))

    if scenario in _ALWAYS_ABORT:
        behaviour = "abort"

    if behaviour not in _VALID_BEHAVIOURS:
        print(
            f"[FAILURE] Unknown behaviour {behaviour!r} for scenario {scenario!r}, defaulting to abort"
        )
        behaviour = "abort"

    if behaviour == "abort":
        print(f"[FAILURE][{scenario}] ABORT: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "error":
        print(f"[FAILURE][{scenario}] ERROR: {message}")
        raise RuntimeError(f"[{scenario}] {message}")
    elif behaviour == "warn_and_fallback":
        print(f"[FAILURE][{scenario}] WARNING: {message} — falling back")
        return fallback
    elif behaviour == "fallback":
        return fallback


# ---------------------------------------------------------------------------
# Conf file emission
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def emit_makepkg_conf(resolved_profile, active_consumes=None):
    """
    Write makepkg-relevant keys from resolved_profile to a temp file.
    Yields the path to the temp file for use with MAKEPKG_CONF.
    Cleans up the temp file on exit.

    Only keys belonging to conf types in active_consumes are written.
    If active_consumes is None, falls back to writing all non-internal keys
    (backward-compatible behaviour).

    Keys absent from all _CONF_KEY_MAP entries and not in _SYSFORGE_KEYS are
    held back for the future env pass and logged as skipped.
    """
    if active_consumes is None:
        allowed_keys = None
    else:
        allowed_keys: set[str] = set()
        for conf_type in active_consumes:
            if conf_type in _CONF_KEY_MAP:
                allowed_keys.update(_CONF_KEY_MAP[conf_type])

    conf_lines = []
    skipped_for_env: list[str] = []

    for key, val in resolved_profile.items():
        if key in _SYSFORGE_KEYS:
            continue
        if allowed_keys is not None and key not in allowed_keys:
            skipped_for_env.append(key)
            continue
        conf_lines.append(f'{key}="{val}"')

    if skipped_for_env:
        print(
            f"[CONF] Skipped (env pass, not yet implemented): "
            f"{sorted(skipped_for_env)}"
        )

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
# Build invocation
# ---------------------------------------------------------------------------

def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile):
    pkgbuild_path = Path(pkgbuild_path).resolve()
    build_dir = pkgbuild_path.parent

    env = os.environ.copy()
    env["MAKEPKG_CONF"] = str(conf_path)

    flags = resolved_profile.get("makepkg_flags", [])
    cmd = ["makepkg"] + flags

    print(
        f"[BUILD] Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}"
    )

    result = subprocess.run(cmd, cwd=build_dir, env=env)

    if result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, "makepkg")


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.
    """
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile)
        except subprocess.CalledProcessError as e:
            print(f"[BUILD] Build failed in batch mode, aborting: {e}")
            raise RuntimeError(f"[build_failed] {e}")
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile)
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
        with emit_makepkg_conf(resolved_profile, active_consumes) as conf_path:
            _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile)
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
        _run_build(
            pkgbuild_path, resolved_profile, config, groups,
            active_consumes=active_consumes,
            extracted_profile=extracted_profile if build_mode == "patch_pkgbuild" else None,
            pkgmeta=pkgmeta,
        )


if __name__ == "__main__":
    run(sys.argv[1])
