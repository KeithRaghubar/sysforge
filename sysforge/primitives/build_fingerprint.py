# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_fingerprint.py — input-fingerprint reuse for the PGO toolchain (Pass 3).

The PGO toolchain build (``sysforge run toolchain``) is the only multi-package,
multi-hour build in the suite. When a *late* Pass-3 package fails, rerunning
rebuilds the already-successful, byte-for-byte-identical earlier Pass-3 packages
(notably ``llvm``/``llvm-libs``, the heaviest target). This module lets the
stage *skip* a package whose **inputs are unchanged** — keyed not by an
impossible-to-predict output hash, but by a fingerprint of everything that feeds
the build: the PKGBUILD recipe, the source commit, the PGO profdata content, the
compiler identity, the injected/profile flags, and the installed build-dep
versions. Pass-3 sub-passes are chained Merkle-style (a consumer folds in its
staged deps' fingerprints) so no stale libLLVM can ride through a cache hit.

Correctness posture — this is the module that guards against silent
mis-optimisation, so every operation **fails safe**: a missing artifact, an
unreadable file, a changed on-disk artifact, a schema bump, or any I/O error
yields a cache *miss* (rebuild), never a false hit. Reuse is opt-in at the call
site; this module only ever *answers* "is the recorded artifact still valid for
these exact inputs?".

The cache is a small JSON file (typically ``<pgo_store>/build_cache.json``) so
it auto-invalidates when a fresh 4-pass build purges ``pgo_store`` and
auto-persists across a profdata-reuse resume.

Public API:
    compute_fingerprint(components)            -> str
    cache_key(pass_id, pkgbase)                -> str
    load_cache(path) / save_cache(path, cache)
    cache_hit(cache, key, fp, search_dirs, pkgname)    -> Path | None
    record_build(cache, key, fp, search_dirs, pkgname) -> Path | None
    hash_file(path) / hash_obj(obj)            -> str | None / str
    source_commit(pkgbuild_dir)                -> str | None
    clang_identity(cc)                         -> str
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

# Fingerprint schema version. Bump whenever the set of inputs folded into a
# fingerprint changes, so every previously cached entry is invalidated (a stale
# entry computed under the old input set would otherwise risk a false hit).
_SCHEMA = 1

# Chunk size for streaming file hashes (profdata can be hundreds of MiB).
_HASH_CHUNK = 1024 * 1024


def hash_file(path) -> str | None:
    """sha256 hex digest of a file's contents, or ``None`` if unreadable.

    Streamed so a large ``clang.profdata`` doesn't load into memory. A ``None``
    return propagates into the fingerprint as a distinct value, so a vanished
    input never silently collides with a present one.
    """
    try:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(_HASH_CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def hash_obj(obj) -> str:
    """sha256 hex digest of a canonical JSON serialization of ``obj``.

    ``sort_keys`` makes dict ordering irrelevant; ``default=str`` tolerates
    non-JSON scalars (e.g. ``datetime`` from a parsed TOML config).
    """
    payload = json.dumps(obj, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def source_commit(pkgbuild_dir) -> str | None:
    """Resolved git HEAD of the PKGBUILD's source tree, or ``None``.

    ``None`` when the directory is not a git checkout (a hand-maintained local
    PKGBUILD) — in that case the PKGBUILD content hash is the source-of-truth
    and the commit dimension simply stays constant.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(pkgbuild_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def compiler_version_line(cc) -> str:
    """First line of ``<cc> --version``, or ``""`` if unobtainable.

    Never raises — a missing binary, a timeout, or a mocked ``subprocess.run``
    (which may return a non-str stdout under test) all degrade to ``""``.
    """
    if not cc:
        return ""
    try:
        proc = subprocess.run(
            [str(cc), "--version"], capture_output=True, text=True,
            timeout=10, check=False,
        )
        out = proc.stdout if isinstance(proc.stdout, str) else ""
        return out.splitlines()[0].strip() if out else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def clang_identity(cc) -> str:
    """Stable identity string for the compiler at path ``cc``.

    Combines the path, the binary's size + nanosecond mtime, and the first line
    of ``--version``. The size/mtime catches a same-version reinstall or rebuild
    of the compiler between runs; the version line catches a version bump. Never
    raises — degrades to whatever fields are obtainable.
    """
    if not cc:
        return "none"
    parts = [str(cc)]
    try:
        st = os.stat(cc)
        parts.append(f"size={st.st_size}")
        parts.append(f"mtime={st.st_mtime_ns}")
    except OSError:
        pass
    first = compiler_version_line(cc)
    if first:
        parts.append(first)
    return "|".join(p for p in parts if isinstance(p, str))


def resolve_libllvm(cc):
    """Resolve the ``libLLVM.so*`` shared object shipped alongside ``cc``.

    The clang driver at ``<prefix>/bin/clang`` links ``<prefix>/lib/libLLVM.so``
    dynamically, so libLLVM is the real codegen carrier — a PGO rebuild changes
    its bytes even when the driver's bytes (and version line) are unchanged.
    Returns the resolved ``Path`` or ``None`` when no libLLVM is found (e.g. a
    gcc compiler, or a static/unusual layout). Never raises.
    """
    if not cc:
        return None
    try:
        prefix = Path(cc).resolve().parent.parent
    except (OSError, RuntimeError):
        return None
    for libdir in (prefix / "lib", prefix / "lib64"):
        try:
            matches = sorted(libdir.glob("libLLVM.so*"))
        except OSError:
            continue
        if matches:
            return matches[0]
    return None


def toolchain_fingerprint(method, cc) -> str:
    """Opaque identity string for the active toolchain, selected by ``method``.

    ``method`` mirrors ``[toolchain] drift_detect``:

    - ``"content_hash"`` — sha256 of the resolved ``libLLVM.so`` mixed with the
      compiler ``--version`` line. Precise: catches a same-version libLLVM PGO
      rebuild the stat-based method would miss, at the cost of hashing a large
      shared object. Falls back to ``clang_identity`` when no libLLVM resolves
      (e.g. a gcc variant) so it never crashes.
    - ``"fingerprint"`` (default) and any unrecognised value — ``clang_identity``
      (path + size + mtime + version line). Fast, no hashing.

    The value is opaque and compared only for equality, so a ``drift_detect``
    flip self-heals: stamped strings simply stop matching the new method's
    output, and the next rebuild re-stamps.
    """
    if method == "content_hash":
        so = resolve_libllvm(cc)
        if so is not None:
            digest = hash_file(so)
            if digest is not None:
                return f"content_hash|{digest}|{compiler_version_line(cc)}"
        return clang_identity(cc)
    return clang_identity(cc)


def compute_fingerprint(components: dict) -> str:
    """Fingerprint a build from its input ``components`` dict.

    ``components`` is built by the caller and is expected to carry (keys are
    advisory — any extra key participates, any missing key is just absent):

      pass_id              — sub-pass identity ("3a"/"3b"/"3c") so the same
                             pkgbase built differently across passes never
                             collides.
      pkgbase              — package identity.
      pkgbuild_sha         — hash_file() of the upstream PKGBUILD.
      source_commit        — source_commit() of the PKGBUILD dir.
      cc_identity          — clang_identity() of the build compiler.
      compiler_flags_extra / linker_flags_extra / cmake_llvm_dir / extra_flags
                           — the per-pass injected flags.
      config_digest        — hash_obj() of the flag-relevant config subset.
      profdata_sha         — hash_file() of clang.profdata (Pass 3 only).
      makedep_versions     — {dep: installed_version} of the build deps.
      staged_dep_fps       — sorted fingerprints of staged sibling builds
                             (Merkle chain: 3b/3c fold in 3a's fingerprints).

    The ``_schema`` constant is mixed in so an input-set change invalidates all
    prior cache entries.
    """
    return hash_obj({"_schema": _SCHEMA, **components})


def cache_key(pass_id: str, pkgbase: str) -> str:
    """Stable cache key for a (sub-pass, pkgbase) pair.

    A NUL separator can't appear in a pass id or pkgbase, so the join is
    unambiguous.
    """
    return f"{pass_id}\x00{pkgbase}"


def load_cache(path) -> dict:
    """Load the JSON build cache, or ``{}`` on any error (fail safe)."""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(path, cache) -> None:
    """Atomically write the build cache (write temp + rename)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, indent=2, sort_keys=True))
    tmp.replace(p)


def _newest_artifact(search_dirs, pkgname) -> Path | None:
    """Newest ``{pkgname}-<ver>-*.pkg.tar*`` across ``search_dirs``, else None.

    Version-anchored glob (``-[0-9]``) — mirrors ``_extract_pass2_to_staging`` /
    ``pacman.cached_pkg_files_for`` so a shorter pkgname (``llvm``) never
    swallows a longer sibling (``llvm-libs``). Signatures excluded.
    """
    for d in search_dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        cands = [
            p for p in d.glob(f"{pkgname}-[0-9]*-*.pkg.tar*")
            if not p.name.endswith(".sig")
        ]
        if cands:
            return max(cands, key=lambda p: p.stat().st_mtime)
    return None


def record_build(cache, key, fingerprint, search_dirs, pkgnames) -> list[Path]:
    """Record the just-built artifacts for one PKGBUILD under ``key``.

    ``pkgnames`` are all the package names a single makepkg invocation produced
    (split packages share a build, e.g. ``["llvm", "llvm-libs"]``). Each
    artifact's path, nanosecond mtime, and size are stored so a later
    ``cache_hit`` can prove every member is present and unchanged — skipping a
    build only when *all* outputs are still on disk. Returns the recorded
    artifact paths (empty if none were found, in which case nothing is cached).
    """
    entries: list[dict] = []
    arts: list[Path] = []
    for pkgname in pkgnames:
        art = _newest_artifact(search_dirs, pkgname)
        if art is None:
            continue
        try:
            st = art.stat()
        except OSError:
            continue
        entries.append({
            "pkgname": pkgname,
            "artifact": str(art),
            "mtime": st.st_mtime_ns,
            "size": st.st_size,
        })
        arts.append(art)
    if not entries:
        return []
    cache[key] = {"fingerprint": fingerprint, "artifacts": entries}
    return arts


def cache_hit(cache, key, fingerprint) -> list[Path] | None:
    """Return the cached artifacts iff still valid for ``fingerprint``, else None.

    A hit requires all of: a cache entry under ``key``; a matching fingerprint;
    and *every* recorded split-member artifact still present with unchanged size
    + nanosecond mtime. Any deviation → ``None`` (rebuild). The check binds to
    the *recorded* artifact paths so a stale sibling from a different pass can
    never be mistaken for the cached output.
    """
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    if entry.get("fingerprint") != fingerprint:
        return None
    arts = entry.get("artifacts")
    if not isinstance(arts, list) or not arts:
        return None
    out: list[Path] = []
    for a in arts:
        if not isinstance(a, dict) or not a.get("artifact"):
            return None
        path = Path(a["artifact"])
        try:
            st = path.stat()
        except OSError:
            return None
        if st.st_mtime_ns != a.get("mtime") or st.st_size != a.get("size"):
            return None
        out.append(path)
    return out
