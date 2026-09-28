# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_prep.py — pre-build source acquisition and signing-key setup

Three steps that run *before* makepkg is invoked on a packaging repo:

    pkgctl_checkout(name, dest)      -> None   pkgctl repo clone of the official
                                               Arch packaging repo into dest
    pkgctl_switch_version(dest, ver) -> None   pin a cloned repo to a release tag
    import_pgp_keys(pkgmeta, ...)    -> None   ensure validpgpkeys are present in
                                               the GPG keyring (bundled + keyserver)

All are pure side-effecting helpers over external tools (``pkgctl`` / ``gpg``)
with no AUR-RPC concern, which is why they live apart from ``aur.py``.
``pkgctl_checkout`` reuses ``git_ops.purge_src`` to clear a half-cloned
leftover before re-cloning.

``aur.py`` re-exports all three so existing call sites and tests are unchanged.
"""
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from sysforge import log
from sysforge.primitives.git_ops import purge_src
from sysforge.primitives.net_policy import KIND_KEY_FETCH, KIND_REPO_CHECKOUT, get_policy

# [BUILD_PREP], not [BUILD]: this module does pre-build *acquisition* (clone the
# packaging repo via pkgctl, import validpgpkeys) — it never compiles anything.
# The build subsystem ([BUILD]: build_core + makepkg_wrapper) owns the build
# itself; sharing its tag here was a leftover from when this code lived in aur.py.
_log = log.get_logger("BUILD_PREP")


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

    Source freeze (3.0.0-F2): checked as the *first* statement — this is a
    code-ingress seam exactly like ``aur_clone``, just from a different origin.
    The scheduler's ``_clone`` converts the raise into ``STATUS_FROZEN``;
    ``config.find_pkgbuild`` lets it propagate.
    """
    get_policy().check(KIND_REPO_CHECKOUT, name)

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
        ) from None

    output_lines: list[str] = []
    proc_stdout = proc.stdout
    assert proc_stdout is not None  # noqa: S101 — internal invariant (stdout=PIPE guarantees it), not input validation

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
        ) from None
    drainer.join(timeout=1)

    if proc.returncode != 0:
        raise RuntimeError(
            f"pkgctl checkout failed for {name!r}:\n" + "\n".join(output_lines).strip()
        )


def pkgctl_switch_version(dest: Path, version: str, *, timeout: int | None = 60) -> None:
    """Switch a pkgctl checkout to the release tag matching a pacman version.

    ``pkgctl repo switch <version>`` owns the pacman-version → git-tag
    translation (epoch and pkgrel included), so callers pass
    ``pacman.get_repo_candidate_version()`` output verbatim. Leaves the checkout
    on a detached HEAD at the tag. Raises RuntimeError on failure.
    """
    timeout = timeout or None
    try:
        result = subprocess.run(
            ["pkgctl", "repo", "switch", version],
            cwd=str(dest),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except FileNotFoundError:
        raise RuntimeError(
            "pkgctl not found on PATH. Install it with: sudo pacman -S --needed devtools"
        ) from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"pkgctl repo switch {version!r} timed out after {timeout}s"
        ) from None
    if result.returncode != 0:
        raise RuntimeError(
            f"pkgctl repo switch {version!r} failed: {(result.stderr or result.stdout).strip()}"
        )
    _log.info(f"Pinned {dest.name} to release {version}")


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

    # Step 3: fetch remaining keys from a keyserver — consent-gated (3.1.0-F8)
    _log.info(f"GPG: {len(missing)}/{len(keys)} key(s) missing, fetching via keyserver")
    fetch_pgp_keys(missing, pkgbase=pkgbase_from_globals(pkgmeta.get("globals", {})))


def pkgbase_from_globals(globals_: dict) -> str | None:
    """makepkg's rule over parsed PKGBUILD globals: ``pkgbase``, else
    ``pkgname[0]``; ``None`` when neither is a usable string."""
    pkgbase = globals_.get("pkgbase")
    if isinstance(pkgbase, str) and pkgbase:
        return pkgbase
    names = globals_.get("pkgname")
    first = names[0] if isinstance(names, list) and names else names
    return first if isinstance(first, str) and first else None


# ``[security] auto_fetch_pgp_keys`` — installed once by cli.main (like the
# sandbox policy); False means confirm on a TTY and fail closed otherwise.
_AUTO_FETCH_KEYS = False


def set_key_fetch_policy(*, auto: bool) -> None:
    global _AUTO_FETCH_KEYS
    _AUTO_FETCH_KEYS = bool(auto)


def _describe_keys(homedir: str) -> list[tuple[str, str]]:
    """``(fingerprint, primary uid)`` for every key in a throwaway keyring."""
    r = subprocess.run(
        ["gpg", "--homedir", homedir, "--batch", "--with-colons",
         "--fingerprint", "--list-keys"],
        capture_output=True, text=True,
    )
    out: list[tuple[str, str]] = []
    fpr = None
    for line in (r.stdout or "").splitlines():
        fields = line.split(":")
        if fields[0] == "pub":
            fpr = None
        elif fields[0] == "fpr" and fpr is None and len(fields) > 9:
            fpr = fields[9]
            out.append((fpr, ""))
        elif fields[0] == "uid" and out and not out[-1][1] and len(fields) > 9:
            out[-1] = (out[-1][0], fields[9])
    return out


def _consent_to_import(pkgbase: str | None) -> bool:
    from sysforge.primitives import prompt

    if _AUTO_FETCH_KEYS:
        return True
    if not prompt.is_interactive():
        return False
    answer = prompt.prompt_choice(
        f"Trust these key(s) as upstream signers for {pkgbase or 'this package'} "
        "and import them into your keyring? [y/N] ",
        ["y", "n"], default="n", retry_on_invalid=False, tag="GPG",
    )
    return answer == "y"


def fetch_pgp_keys(fingerprints: list[str], *, pkgbase: str | None) -> list[str]:
    """Fetch ``validpgpkeys`` from a keyserver — only with consent (3.1.0-F8).

    ``validpgpkeys`` exists so a *human* decides who the upstream signer is;
    importing whatever a PKGBUILD names lets a tampered PKGBUILD ship its own
    key beside a re-signed tarball and pass makepkg's verification. So:

    * the fetch is its own egress kind (``KIND_KEY_FETCH``), so the source
      freeze covers it — raises ``NetworkFrozen`` like every other seam;
    * keys land in a throwaway ``GNUPGHOME`` first, and fingerprint, owner and
      requesting pkgbase are shown before anything touches the real keyring;
    * the import needs confirmation on a TTY; a non-interactive run fails
      closed (nothing imported — makepkg then reports the missing key) unless
      ``[security] auto_fetch_pgp_keys = true`` opts back into unattended use.

    The one home for keyserver fetches (``auto_repair`` routes through it).
    Returns the fingerprints actually imported.
    """
    get_policy().check(KIND_KEY_FETCH, pkgbase)
    with tempfile.TemporaryDirectory(prefix="sysforge-gpg-") as home:
        Path(home).chmod(0o700)
        r = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--recv-keys", *fingerprints],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            _log.warn(f"GPG: keyserver fetch failed:\n{(r.stderr or '').strip()}")
            return []
        described = _describe_keys(home) or [(f, "") for f in fingerprints]
        for fpr, uid in described:
            _log.ui(
                f"[GPG] {pkgbase or '<unknown>'} names signer {fpr}"
                f"{f' ({uid})' if uid else ''} — not in your keyring"
            )
        fprs = [fpr for fpr, _ in described]
        if not _consent_to_import(pkgbase):
            _log.error(
                f"GPG: not importing {', '.join(fprs)} for {pkgbase or 'this package'} "
                "without confirmation. Check the fingerprint against upstream, then "
                f"`gpg --recv-keys {' '.join(fprs)}`, or set [security] "
                "auto_fetch_pgp_keys = true for unattended runs."
            )
            return []
        exported = subprocess.run(
            ["gpg", "--homedir", home, "--batch", "--export", *fprs],
            capture_output=True,
        )
        imported = subprocess.run(
            ["gpg", "--batch", "--import"], input=exported.stdout, capture_output=True,
        )
        if exported.returncode != 0 or imported.returncode != 0:
            _log.warn(f"GPG: importing {', '.join(fprs)} into the keyring failed")
            return []
    _log.info(f"GPG: imported {', '.join(fprs)} for {pkgbase or 'this package'}")
    return fprs
