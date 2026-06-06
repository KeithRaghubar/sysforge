"""
build_prep.py — pre-build source acquisition and signing-key setup

Two steps that run *before* makepkg is invoked on a packaging repo:

    pkgctl_checkout(name, dest)   -> None   pkgctl repo clone of the official
                                            Arch packaging repo into dest
    import_pgp_keys(pkgmeta, ...) -> None   ensure validpgpkeys are present in
                                            the GPG keyring (bundled + keyserver)

Both are pure side-effecting helpers over external tools (``pkgctl`` / ``gpg``)
with no AUR-RPC concern, which is why they live apart from ``aur.py``.
``pkgctl_checkout`` reuses ``git_ops.purge_src`` to clear a half-cloned
leftover before re-cloning.

``aur.py`` re-exports both so existing
``from sysforge.primitives.aur import pkgctl_checkout`` /
``import_pgp_keys`` call sites and tests are unchanged.
"""
import os
import shutil
import subprocess
import threading
from pathlib import Path

from sysforge import log
from sysforge.primitives.git_ops import purge_src

_log = log.get_logger("BUILD")


def pkgctl_checkout(name: str, dest: Path, *, timeout: int | None = 60) -> None:
    """
    Clone the official Arch Linux packaging repo for name into dest via pkgctl.

    pkgctl repo clone <name> run in dest.parent creates dest.parent/<name>/PKGBUILD.
    Raises RuntimeError on failure or timeout.

    Output is streamed line-by-line to the build log so progress is visible at
    -vvv on slow networks (cloning from gitlab.archlinux.org can take minutes).

    If ``dest`` exists but has no PKGBUILD, it's a leftover from an aborted
    prior clone — pkgctl exits 0 with "Skip cloning: Directory exists" in
    that case, silently masking the missing checkout. Purge first so the
    re-clone runs (purge_src refuses if the leftover has uncommitted work).
    """
    timeout = timeout or None  # 0 → disable
    if (dest.exists()
            and (dest / ".git").exists()
            and not (dest / "PKGBUILD").exists()):
        purge_src(dest)
    _log.info(f"Checking out {name!r} from official repos → {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.Popen(
            ["pkgctl", "repo", "clone", "--protocol=https", name],
            cwd=str(dest.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pkgctl not found on PATH. Install it with: sudo pacman -S --needed devtools"
        )

    output_lines: list[str] = []
    proc_stdout = proc.stdout
    assert proc_stdout is not None  # set above by stdout=subprocess.PIPE

    def _drain():
        for line in proc_stdout:
            stripped = line.rstrip()
            output_lines.append(stripped)
            _log.debug(stripped)

    drainer = threading.Thread(target=_drain, daemon=True)
    drainer.start()
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        drainer.join(timeout=1)
        shutil.rmtree(dest, ignore_errors=True)
        raise RuntimeError(
            f"pkgctl checkout timed out after {timeout}s for {name!r}. "
            "Increase [git] clone_timeout in sysforge.toml on slow networks."
        )
    drainer.join(timeout=1)

    if proc.returncode != 0:
        raise RuntimeError(
            f"pkgctl checkout failed for {name!r}:\n" + "\n".join(output_lines).strip()
        )


def import_pgp_keys(pkgmeta: dict, pkgbuild_path: Path) -> None:
    """
    Ensure all validpgpkeys in pkgmeta are present in the GPG keyring.

    Strategy (in order):
    1. Import any bundled .asc files from keys/pgp/ next to the PKGBUILD.
    2. Check which validpgpkeys are still missing from the keyring.
    3. Fetch any remaining missing keys via gpg --recv-keys.

    Import/fetch failures are logged as warnings — makepkg will surface a
    clearer error if a key is still absent when signature verification runs.
    """
    keys = pkgmeta.get("globals", {}).get("validpgpkeys", [])
    if not keys:
        return

    _log.info(f"GPG: {len(keys)} validpgpkey(s) required")

    # Step 1: import bundled keys from keys/pgp/ if present
    keys_dir = pkgbuild_path.parent / "keys" / "pgp"
    if keys_dir.is_dir():
        asc_files = sorted(keys_dir.glob("*.asc"))
        if asc_files:
            _log.info(f"GPG: importing {len(asc_files)} bundled key(s) from {keys_dir}")
            r = subprocess.run(
                ["gpg", "--import", *[str(f) for f in asc_files]],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                _log.warn(f"GPG: bundled import failed:\n{r.stderr.strip()}")
            else:
                _log.info("GPG: bundled import succeeded")

    # Step 2: check which keys are still missing
    missing = [
        key for key in keys
        if subprocess.run(["gpg", "--list-keys", key], capture_output=True).returncode != 0
    ]

    if not missing:
        _log.info(f"GPG: all {len(keys)} key(s) present in keyring")
        return

    # Step 3: fetch remaining keys from keyserver
    _log.info(f"GPG: {len(missing)}/{len(keys)} key(s) missing, fetching via keyserver")
    r = subprocess.run(["gpg", "--recv-keys", *missing], capture_output=True, text=True)
    if r.returncode != 0:
        _log.warn(f"GPG: keyserver fetch failed:\n{r.stderr.strip()}")
    else:
        _log.info("GPG: keyserver fetch succeeded")
