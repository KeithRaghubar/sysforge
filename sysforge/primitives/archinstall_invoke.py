# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT
"""primitives/archinstall_invoke.py — the sole side-effecting archinstall seam.

Writes the generated config to a 0600 temp file and runs
``archinstall --config <file> --silent``. archinstall is only present on the
live ISO, so a which() preflight gates the whole feature; never imported.
"""
import copy
import json
import os
import shutil
import subprocess
import tempfile

from sysforge import log
from sysforge.primitives.run import run_or_raise
from sysforge.primitives.archinstall_config import ARCHINSTALL_SCHEMA_VERSION

_log = log.get_logger("INSTALL")


def _redact(cfg_dict: dict) -> dict:
    red = copy.deepcopy(cfg_dict)
    if "!root-password" in red:
        red["!root-password"] = "***"
    for u in red.get("users", []):
        if "!password" in u:
            u["!password"] = "***"
    return red


def _warn_on_version_drift() -> None:
    try:
        out = subprocess.run(["archinstall", "--version"], capture_output=True, text=True)
        ver = (out.stdout or out.stderr).strip().split()[-1]
    except Exception:  # noqa: BLE001 — version probe is best-effort
        return
    want = ".".join(ARCHINSTALL_SCHEMA_VERSION.split(".")[:2])
    if not ver.startswith(want):
        _log.warn(
            f"archinstall {ver} differs from the schema this build targets "
            f"({ARCHINSTALL_SCHEMA_VERSION}); config may need regenerating."
        )


def run_archinstall(cfg_dict: dict, *, dry_run: bool) -> None:
    if shutil.which("archinstall") is None:
        raise RuntimeError(
            "[INSTALL] archinstall not found. The bootstrap install stage must run "
            "from the Arch live ISO, where archinstall is available."
        )
    if dry_run:
        print("[dry-run] archinstall config (passwords redacted):")
        print(json.dumps(_redact(cfg_dict), indent=2))
        print("[dry-run] would run: archinstall --config <tmp> --silent")
        return

    _warn_on_version_drift()
    fd, path = tempfile.mkstemp(prefix="sysforge-archinstall-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg_dict, f)
        _log.ui("Running archinstall (this partitions the disk and installs the base system)...")
        run_or_raise(
            ["archinstall", "--config", path, "--silent"],
            tag="INSTALL", operation="archinstall", capture=False,
            hint="Check the archinstall log for the failing step.",
        )
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
