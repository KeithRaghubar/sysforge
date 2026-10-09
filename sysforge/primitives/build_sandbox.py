# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
build_sandbox.py — the build sandbox: blast-radius containment for makepkg.

Every ``prepare()`` / ``build()`` / ``package()`` in a PKGBUILD is arbitrary
code that sysforge executes.  On the default host path it runs as the invoking
user, which means a poisoned PKGBUILD can read ``~/.ssh``, GPG material and
browser profiles — the actual execution vector of the AUR supply-chain
campaigns, where the payload runs at *build time* and never has to land in a
package at all.  ``[security] freeze_sources`` gates *code ingress*; this
module gates *blast radius*.  The two are orthogonal and compose.

The mechanism is devtools' ``makechrootpkg``: the build runs inside a clean
``systemd-nspawn`` container as an unprivileged ``builduser`` with only the
PKGBUILD directory bind-mounted.  It is **opt-in and default-off**
(``[security] sandbox_builds``, per-profile override ``sandbox_builds``) —
the host path stays the tested default.

Why this is not just another ``build_throttle.wrapper_argv`` prefix entry
(the whole reason the retrofit is invasive):

* ``makechrootpkg`` operates on the *current directory* and hardcodes
  ``source PKGBUILD`` for its pkgbase probe, so it cannot honour an arbitrary
  ``-p <name>`` — while sysforge always builds the patched ``PKGBUILD.sysforge``
  sidecar, never the upstream file.  :func:`as_canonical_pkgbuild` reconciles
  the two with a scoped rename around the build, undone in a ``finally``.
* It re-execs itself under ``sudo`` (devtools' ``check_root``) with its own
  env-preserve list, so sysforge must *not* prefix ``sudo`` — doing so would
  strip ``PKGDEST``/``SRCDEST``/``LOGDEST``/``MAKEFLAGS`` from the environment
  and silently relocate every artifact.  That is why this seam is exempt from
  the ``privileged_argv`` rule: the escalation is the tool's own, and wrapping
  it breaks it.
* ``MAKEPKG_CONF`` does not cross the container boundary, and neither do the
  env-delivered ``CC``/``CXX`` that ``CONF_KEY_MAP["toolchain"]`` deliberately
  keeps *out* of the conf file.  :func:`chroot_conf_text` therefore derives a
  container-side conf from the emitted one: dest keys repointed at the
  container's ``/pkgdest`` & co, and the env-delivered keys re-added as
  ``export`` lines (the conf is sourced by bash, so an export in it is the one
  channel that survives ``sudo -iu builduser``).
* The throttle wrapper applies *outside* the container (nice/ionice propagate
  to children; a ``RLIMIT_AS`` cap would only bind ``makechrootpkg`` itself,
  not the build — :func:`mem_cap_applies` says so, and the caller warns).
* Dependencies built earlier in the same run are installed on the *host*, so
  the container cannot see them.  The session registry
  (:func:`register_artifacts` / :func:`install_args`) feeds them back in as
  ``-I`` so an AUR dep chain still resolves.

Public API:
    SandboxUnavailable
    SandboxPolicy(enabled, chroot_dir, clean, update, local_repo).describe()
    resolve_sandbox(cfg) -> SandboxPolicy
    for_profile(policy, resolved_profile) -> SandboxPolicy
    set_policy(policy) / get_policy() / reset_policy() / suppressed(active)
    preflight(policy, pkgbuild_path) -> None      (raises SandboxUnavailable)
    as_canonical_pkgbuild(pkgbuild_path) -> ctx  (yields ./PKGBUILD)
    build_argv(policy, flags, conf_dir_name, install_pkgs) -> list[str]
    dest_env_from_conf(conf_path) -> dict
    chroot_conf_text(conf_path, exports) -> str
    audit_chroot_conf(conf_text, policy) -> None   (raises SandboxUnavailable)
    chroot_env(env) -> dict
    mem_cap_applies(policy) -> bool
    missing_toolchain(policy, exports) -> dict
    provision_toolchain(policy, exports) -> None
    register_artifacts(paths) / install_args(pkgbuild, ..., exports, conf_path) / reset_session()
    toolchain_packages(exports, conf_path) -> set
    installed_artifact(name, search_dir) -> Path | None
    COPY_NAME; SandboxProvenance(cc_name, copy_identity)
    set_provenance(p) / take_provenance() / clear_provenance()
    copy_compiler(policy, cc_name) -> Path
    recorded_toolchain(prov, host_variant, host_fingerprint) -> (variant, fingerprint)
    resolve_dep_artifacts(pkgbuild, search_dir, state_dir) -> list[Path]
"""
from __future__ import annotations

import contextlib
import re
import shlex
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from sysforge import log
from sysforge.primitives.privilege import run_privileged

_log = log.get_logger("SANDBOX")

# The one filename ``makechrootpkg`` can build: it bind-mounts the invocation
# directory and runs ``source PKGBUILD`` on the host to read pkgbase, so a
# ``-p <other>`` would build one file and report the other's name.
REQUIRED_PKGBUILD_NAME = "PKGBUILD"

# The container-side conf lives in a scratch directory *inside* the PKGBUILD
# directory, because that directory is the one thing ``makechrootpkg``
# bind-mounts (at ``/startdir``) — reaching it needs no extra ``-D`` mount and
# so cannot collide with the ``--tmpfs=/tmp`` the container already gets. Both
# names are dotted so they never match the ``*.pkg.tar*`` / ``PKGBUILD*`` globs
# the rest of the tree runs over that directory, and the whole scratch dir is
# removed in the caller's ``finally``.
# Where the upstream PKGBUILD is parked while the patched sidecar occupies the
# canonical name (see :func:`as_canonical_pkgbuild`). Dotted for the same
# reason as the names below: the rest of the tree globs ``PKGBUILD*`` over this
# directory, and an undotted stash would read as a second build candidate.
UPSTREAM_STASH_NAME = ".sysforge-upstream-PKGBUILD"

CHROOT_CONF_NAME = ".sysforge-chroot-makepkg.conf"
CHROOT_CONF_DIR_PREFIX = ".sysforge-chroot-"
CHROOT_STARTDIR = "/startdir"

# The working copy's name, passed as ``makechrootpkg -l`` so its path is known
# rather than inherited from $USER (3.4.0-B3): provenance fingerprints the
# compiler in <chroot>/<COPY_NAME>, which is what actually built the package.
COPY_NAME = "sysforge"
# Where the host's own compiler lives for the provenance comparison. Named
# explicitly, not resolved via PATH: a ccache wrapper directory first on PATH
# would fingerprint the wrapper instead of the compiler.
_HOST_BIN_DIR = Path("/usr/bin")

# Where the container's makepkg.conf must point its dest keys. These are the
# paths ``makechrootpkg`` creates inside the working copy and moves artifacts
# out of afterwards; a conf that kept the host values would have makepkg write
# into paths that do not exist in the container.
_CHROOT_DEST_KEYS = {
    "BUILDDIR": "/build",
    "PKGDEST": "/pkgdest",
    "SRCPKGDEST": "/srcpkgdest",
    "SRCDEST": "/srcdest",
    "LOGDEST": "/logdest",
}

# Env keys that are meaningless or actively wrong inside the container: host
# paths, sysforge's own plumbing, and the conf pointer the container replaces.
_ENV_EXPORT_DENY = {
    "MAKEPKG_CONF", "PATH", "HOME", "PWD", "OLDPWD", "SHELL", "USER", "LOGNAME",
    "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "LLVM_PROFILE_FILE",
    "BUILDDIR", "PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST",
    # Host-only compiler caches (3.2.0-B2). RUSTC_WRAPPER names a binary the
    # clean chroot does not carry, so exporting it fails every Rust build the
    # same way an inherited ``ccache`` BUILDENV fails every C one; the *_DIR
    # keys name host paths, which is the category above.
    "RUSTC_WRAPPER", "CCACHE_DIR", "SCCACHE_DIR",
}

# BUILDENV options that name a binary rather than a policy. The sandbox chroot
# is base-devel only, and makepkg treats a missing accelerator as a hard error
# ("Cannot find the ccache binary required for compiler cache usage") raised
# before prepare() — so an inherited host BUILDENV failed every package the
# sandbox was ever pointed at (3.2.0-B2). Disabled rather than installed into
# the chroot: ``makechrootpkg -c`` resets the working copy each build and never
# mounts a cache directory, so the hit rate in there is structurally zero and
# the accelerators buy nothing to trade away. ``check``/``sign`` are policy,
# not tooling, and are left exactly as the user set them.
_HOST_ONLY_BUILDENV = ("ccache", "distcc")

# Env keys whose value *is* a binary name, as opposed to flags or a policy.
# These are what the profile's resolved toolchain travels into the container
# on, via the ``export`` lines :func:`chroot_conf_text` appends.
_TOOLCHAIN_ENV_KEYS = ("CC", "CXX", "LD", "AR", "NM", "RANLIB", "STRIP", "OBJCOPY")

# Flag-valued keys are not binary-valued, with exactly one exception: the
# driver flag ``-fuse-ld=<name>``, which sends clang/gcc looking for
# ``ld.<name>`` at link time. A profile that selects its linker this way never
# sets ``LD``, so the binary probe above cannot see the choice (3.2.0-B6).
# ``-fuse-ld`` is a driver flag rather than a link-only one, so it is equally
# legal in CFLAGS/CXXFLAGS and all three are scanned.
_LINKER_FLAG_KEYS = ("LDFLAGS", "CFLAGS", "CXXFLAGS")
_FUSE_LD_RE = re.compile(r"(?:^|\s)-fuse-ld=(\S+)")

# Which pacman package provides each toolchain binary. The clean chroot is
# ``base-devel``, which carries gcc and binutils and nothing else — so a
# profile resolving to LLVM exports ``CC=clang`` into a container with no
# clang in it, and every C build dies in configure with "Could not find the
# compiler specified in the environment variable CC" (3.2.0-B4). Same category
# as the ccache BUILDENV above, but the opposite remedy: an accelerator is
# free to drop, whereas the compiler *is* the profile's intent, so the missing
# package is installed into the chroot rather than the export dropped.
_TOOLCHAIN_PACKAGE = {
    "clang": "clang", "clang++": "clang", "clang-cpp": "clang",
    "ld.lld": "lld", "lld": "lld", "wasm-ld": "lld",
    "ld.mold": "mold", "mold": "mold",
    "llvm-ar": "llvm", "llvm-nm": "llvm", "llvm-ranlib": "llvm",
    "llvm-strip": "llvm", "llvm-objcopy": "llvm",
    "gcc": "gcc", "g++": "gcc", "cpp": "gcc",
    "ar": "binutils", "nm": "binutils", "ranlib": "binutils",
    "ld": "binutils", "ld.bfd": "binutils", "ld.gold": "binutils",
    "strip": "binutils", "objcopy": "binutils",
}


# makechrootpkg reads these from the environment (devtools' check_root preserve
# list) and moves the built artifacts back to them, so exporting them keeps the
# sandbox path's artifact locations identical to the host path's.
PRESERVED_DEST_ENV = ("PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST")


class SandboxUnavailable(RuntimeError):
    """Raised when a sandboxed build was requested but cannot be run.

    Never downgraded to a host build: the user asked for isolation, and
    silently building without it is the failure mode the whole feature exists
    to prevent.
    """


@dataclass(frozen=True)
class SandboxPolicy:
    """An immutable per-run decision about build isolation."""

    enabled: bool
    chroot_dir: Path | None = None
    clean: bool = True
    update: bool = True
    # 3.1.0-F10: publish the host's installed source-built packages into a
    # read-only local repo the chroot lists first. False here so a hand-built
    # policy does nothing new; resolve_sandbox applies the shipped default.
    local_repo: bool = False

    def describe(self) -> str:
        if not self.enabled:
            return "off (builds run on the host)"
        bits = [f"chroot={self.chroot_dir}"]
        if self.clean:
            bits.append("clean")
        if self.update:
            bits.append("update")
        if self.local_repo:
            bits.append("local-repo")
        return "on (" + ", ".join(bits) + ")"


_PERMISSIVE = SandboxPolicy(enabled=False)
_policy: SandboxPolicy = _PERMISSIVE

DEFAULT_CHROOT_DIR = "~/chroot"


def resolve_sandbox(cfg: dict) -> SandboxPolicy:
    """Resolve the run's policy from the ``[security]`` config section.

    The switch itself can still be overridden per profile — see
    :func:`for_profile`, which is applied at the invocation seam, because the
    profile is only resolved once a specific package is in hand.
    """
    raw_dir = str(cfg.get("sandbox_chroot_dir") or DEFAULT_CHROOT_DIR)
    return SandboxPolicy(
        enabled=bool(cfg.get("sandbox_builds", False)),
        chroot_dir=Path(raw_dir).expanduser(),
        clean=bool(cfg.get("sandbox_clean", True)),
        update=bool(cfg.get("sandbox_update", True)),
        local_repo=bool(cfg.get("sandbox_local_repo", True)),
    )


def for_profile(policy: SandboxPolicy, resolved_profile: dict | None) -> SandboxPolicy:
    """Apply a profile's ``sandbox_builds`` override to *policy*.

    Only the *switch* is per-profile: the chroot location and the clean/update
    knobs describe the machine, not the build, and stay global. The override is
    two-way on purpose — a profile that builds packages the user has audited
    can opt out of the cost, and a profile used for untrusted AUR packages can
    opt in while the global default stays off.
    """
    if not resolved_profile or "sandbox_builds" not in resolved_profile:
        return policy
    return replace(policy, enabled=bool(resolved_profile["sandbox_builds"]))


def set_policy(policy: SandboxPolicy) -> None:
    """Install *policy* as the process-wide sandbox decision."""
    global _policy
    _policy = policy
    _log.info(f"Build sandbox: {policy.describe()}")


def get_policy() -> SandboxPolicy:
    """Return the active policy (permissive when never set)."""
    return _policy


def reset_policy() -> None:
    """Restore the default (host-build) policy. For tests and library use."""
    global _policy
    _policy = _PERMISSIVE


@contextlib.contextmanager
def suppressed(active: bool = True):
    """Force host builds for the duration of the block.

    The exemption for the toolchain and kernel stages: both build *against*,
    and install *into*, the host they are upgrading, so a container copy of the
    system is the wrong target for them by construction (a staged LLVM built in
    a chroot links against that chroot's libraries, and a kernel built there
    cannot see the host's DKMS modules or run its boot audit).

    Scoped rather than threaded because the invocation seam is six call sites
    deep behind the retry and recovery loops, and every one of them would have
    to remember to forward a parameter. ``active=False`` is a no-op, so the
    caller can express the condition inline.
    """
    global _policy
    if not active or not _policy.enabled:
        yield
        return
    previous = _policy
    _policy = _PERMISSIVE
    _log.info("Build sandbox suppressed for this stage build (host toolchain target)")
    try:
        yield
    finally:
        _policy = previous


def preflight(policy: SandboxPolicy, pkgbuild_path: Path) -> None:
    """Raise :class:`SandboxUnavailable` when this build cannot be sandboxed.

    Checks the three things that make the difference between "isolated" and
    "silently not isolated": the tool exists, the clean chroot root exists,
    and the PKGBUILD is named what ``makechrootpkg`` insists it is named.
    Every message names the command that fixes it.
    """
    if not policy.enabled:
        return

    if shutil.which("makechrootpkg") is None:
        raise SandboxUnavailable(
            "[security] sandbox_builds is on but makechrootpkg is not installed "
            "— install it with: pacman -S devtools"
        )

    chroot_dir = policy.chroot_dir
    if chroot_dir is None or not (chroot_dir / "root").is_dir():
        shown = chroot_dir if chroot_dir is not None else "<unset>"
        raise SandboxUnavailable(
            f"[security] sandbox_builds is on but no clean chroot exists at "
            f"{shown}/root — create it with: mkarchroot {shown}/root base-devel "
            f"(or point [security] sandbox_chroot_dir at an existing one)"
        )

    # makechrootpkg can only build the canonical name, and sysforge never
    # hands the seam that name — every build route swaps in the patched
    # ``PKGBUILD.sysforge`` sidecar. :func:`as_canonical_pkgbuild` reconciles
    # the two, so the filename itself is no longer a refusal (3.2.0-B1); what
    # *is* a refusal is a stash left behind by an interrupted earlier run,
    # because completing the swap over it would destroy the user's checkout.
    stash = Path(pkgbuild_path).parent / UPSTREAM_STASH_NAME
    if stash.exists():
        raise SandboxUnavailable(
            f"refusing to sandbox: {stash} is left over from an interrupted "
            f"build, and swapping over it would lose the upstream PKGBUILD "
            f"— restore it with: mv {stash} {stash.parent / REQUIRED_PKGBUILD_NAME}"
        )


def _fuse_ld_names(values) -> list[str]:
    """Binary names for every ``-fuse-ld=`` occurrence across *values*.

    The flag names a linker *flavour*, not the binary: the driver resolves
    ``-fuse-ld=lld`` to ``ld.lld``. An explicit path is taken as written,
    matching how the driver treats it.
    """
    out: list[str] = []
    for raw in values:
        for name in _FUSE_LD_RE.findall(str(raw or "")):
            out.append(name if "/" in name else f"ld.{name}")
    return out


def _conf_flag_values(conf_path) -> list[str]:
    """The flag variables' values out of an emitted makepkg conf.

    ``parse_system_makepkg_conf`` returns each value *verbatim*, surrounding
    quotes included, so a bare ``\\S+`` capture over it yields ``lld"`` and maps
    to no package at all. Unquote before matching.
    """
    if conf_path is None:
        return []
    from sysforge.primitives.config import parse_system_makepkg_conf

    try:
        assignments = parse_system_makepkg_conf(conf_path)
    except Exception:
        return []
    out: list[str] = []
    for key in _LINKER_FLAG_KEYS:
        raw = str(assignments.get(key) or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
            raw = raw[1:-1]
        if raw:
            out.append(raw)
    return out


def missing_toolchain(policy: SandboxPolicy, exports: dict | None,
                      conf_path=None) -> dict:
    """Return ``{binary: package}`` for toolchain binaries absent from the chroot.

    Probes the chroot *root* copy — the template ``makechrootpkg`` clones the
    working copy from — for each binary-valued export. A relative value is
    looked up in ``/usr/bin``; an absolute one is resolved **inside** the
    chroot, never against the host, because that path names a location in the
    container's filesystem and the host almost certainly has a binary of the
    same name sitting at it (which is precisely why this went unnoticed).

    *conf_path* is the emitted host conf, and it is not optional in practice.
    The flag variables never travel in *exports* — ``extra_env`` carries only
    ``CC``/``CXX`` and the profile's env-type keys — so a profile that selects
    its linker with ``LDFLAGS=-fuse-ld=lld`` puts that nowhere the export dict
    can see it. It reaches the container inside the conf body that
    :func:`chroot_conf_text` copies wholesale, which is the only reason the
    build got clang with no linker behind it (3.2.0-B7). Both sources are read:
    a stage may still inject a flag through the environment.
    """
    root = (policy.chroot_dir / "root") if policy.chroot_dir else None
    if root is None or not root.is_dir():
        return {}

    missing: dict[str, str] = {}
    for key in _TOOLCHAIN_ENV_KEYS:
        raw = str((exports or {}).get(key) or "").strip()
        if not raw:
            continue
        # ``CC='clang -m32'`` is a legal value: the binary is the first word.
        try:
            words = shlex.split(raw)
        except ValueError:
            continue
        if not words:
            continue
        value = words[0]
        probe = (root / value.lstrip("/")) if value.startswith("/") \
            else (root / "usr" / "bin" / value)
        if probe.exists():
            continue
        missing[Path(value).name] = _TOOLCHAIN_PACKAGE.get(Path(value).name, "")

    flag_values = [(exports or {}).get(key) for key in _LINKER_FLAG_KEYS]
    flag_values += _conf_flag_values(conf_path)
    for value in _fuse_ld_names(flag_values):
        probe = (root / value.lstrip("/")) if value.startswith("/") \
            else (root / "usr" / "bin" / value)
        if probe.exists():
            continue
        missing[Path(value).name] = _TOOLCHAIN_PACKAGE.get(Path(value).name, "")
    return missing


# Profile-data flags whose value is an *input file* the compiler must read.
# The ``-fprofile-*generate`` family is deliberately absent: those name an
# output directory, so there is nothing to carry in. A container that writes
# profiles and is then torn down is refused outright instead
# (:func:`refuse_profile_generate`, 3.2.0-B12).
_PROFILE_USE_PREFIXES = (
    "-fprofile-use",
    "-fprofile-instr-use",
    "-fprofile-sample-use",
)
_PROFILE_USE_RE = re.compile(
    r"(?:^|\s)(?:" + "|".join(re.escape(p) for p in _PROFILE_USE_PREFIXES) + r")=(\S+)"
)


# Instrumentation flags that make the built program *write* profiles at
# runtime. Matched with or without ``=<dir>``: the bare form writes to the cwd,
# which inside the container is discarded just the same.
_PROFILE_GENERATE_RE = re.compile(
    r"(?:^|\s)(-f(?:cs-)?profile-(?:instr-)?generate)(?:=\S+)?(?=\s|$)"
)


def refuse_profile_generate(policy: SandboxPolicy, exports: dict | None,
                            conf_path=None) -> None:
    """Refuse a profile-*generate* build under the sandbox (3.2.0-B12).

    The instrumented binary writes its ``.profraw`` to the store path baked
    into the flag, which inside the container is the container's path —
    and ``makechrootpkg -c`` discards the working copy at teardown. Nothing
    errors: the build succeeds, the profile is simply gone, and the later
    ``--pgo=use`` finds an empty store or reuses a stale merge. Every other
    sandbox probe asks "is this input present?", which a generate flag, having
    no input, always passes — so this one asks for the flag itself.

    Refusing is the recommended of three directions: carrying the profiles out
    needs a teardown hook that survives a failed build, and a writable
    bind-mount opens a host write channel inside the isolation boundary, which
    needs its own justification. Both channels are read, as elsewhere.
    """
    if not policy.enabled:
        return
    values = [(exports or {}).get(key) for key in _LINKER_FLAG_KEYS]
    values += _conf_flag_values(conf_path)
    found = sorted({
        m for raw in values for m in _PROFILE_GENERATE_RE.findall(str(raw or ""))
    })
    if found:
        raise SandboxUnavailable(
            f"refusing to sandbox a profile-generate build ({', '.join(found)}): "
            "the instrumented package would write its profiles inside the "
            "container, which is discarded at teardown. Run `--pgo=record` "
            "without the sandbox ([security] sandbox_builds = false for this "
            "build), then `--pgo=use` may run sandboxed."
        )


def host_profile_data(exports: dict | None, conf_path=None) -> list:
    """Host profile files named by the flags, in first-seen order.

    The PGO stages bake an absolute host path into the flag
    (``-fprofile-use=/var/cache/sysforge/pgo-mesa/mesa.profdata``), and clang
    treats an unreadable profile as a hard **error** rather than a warning —
    which meson surfaces as ``Compiler clang cannot compile programs``, the
    same message a chroot with no compiler at all produces. That collision is
    why this read as a missing-toolchain problem (3.2.0-B10).

    Both channels are read for the same reason :func:`missing_toolchain` reads
    both, and the result is deduplicated: ``makepkg_conf`` injects one token
    into CFLAGS, CXXFLAGS *and* LDFLAGS, so the naive read returns it three
    times. A path absent on the host is skipped — there is nothing to copy, and
    the host build would fail on it identically, so the sandbox must not invent
    a failure mode of its own for it.
    """
    values = [(exports or {}).get(key) for key in _LINKER_FLAG_KEYS]
    values += _conf_flag_values(conf_path)
    out: list[Path] = []
    for raw in values:
        for match in _PROFILE_USE_RE.findall(str(raw or "")):
            path = Path(match)
            if path in out or not path.is_file():
                continue
            out.append(path)
    return out


def provision_profile_data(policy: SandboxPolicy, exports: dict | None,
                           conf_path=None) -> None:
    """Materialize the flags' profile files inside the chroot.

    Copied to the **same absolute path** the flag already names, so nothing has
    to rewrite the flags on the way in: the token that works on the host works
    unchanged in the container. Into the *root* copy, which ``makechrootpkg -c``
    reseeds the working copy from, matching :func:`provision_toolchain`.

    Unconditional rather than existence-gated: the store is rewritten by every
    ``--pgo=use`` merge, so a mirror left by an earlier build is not evidence of
    a current one, and skipping on presence would quietly pin the container to a
    stale profile — a wrong-output bug rather than a loud one.

    A failed copy is a hard stop. Continuing hands clang a flag naming a file
    that is still absent, which is exactly the cryptic failure this removes;
    dropping the flag instead would build a non-PGO package under a PGO profile,
    the silent downgrade the sandbox refuses on principle.
    """
    if not policy.enabled or not policy.chroot_dir:
        return
    files = host_profile_data(exports, conf_path=conf_path)
    if not files:
        return
    root = policy.chroot_dir / "root"
    for src in files:
        dest = root / str(src).lstrip("/")
        _log.info(f"Mirroring profile data into the sandbox chroot: {src} -> {dest}")
        try:
            # 644: the container builds as builduser, which is not this uid.
            run_privileged(
                ["install", "-Dm644", str(src), str(dest)],
                tag="SANDBOX",
            )
        except Exception as exc:
            raise SandboxUnavailable(
                f"could not copy the profile data {src} into the sandbox "
                f"chroot at {dest}: {exc} — the build would fail with "
                f"clang unable to read {src.name}"
            ) from exc


def provision_toolchain(policy: SandboxPolicy, exports: dict | None,
                        conf_path=None) -> None:
    """Install the profile's toolchain into the chroot root, if it is absent.

    Runs before the build rather than letting the container fail: the failure
    it replaces costs a full source checkout and configure per package and
    surfaces as a CMake/meson error that names neither the sandbox nor the
    profile (3.2.0-B4).

    Installed into the *root* copy, which persists: ``makechrootpkg -c`` resets
    only the working copy, so this is a one-time cost per chroot rather than a
    per-build one. A binary with no known package is a hard stop with the
    command that fixes it — guessing a package name would install the wrong
    thing, and continuing just buys back the cryptic failure.

    The install is ``-Syu``, not ``-S``. Nothing else re-syncs the root copy's
    databases — ``makechrootpkg -c`` reseeds the *working* copy from it — so
    they age indefinitely, and ``arch-nspawn`` hands the container the host's
    mirrorlist while leaving those databases alone. A bare ``-S`` therefore
    resolves a months-old filename against current mirrors and 404s on every
    one of them; ``-Sy`` would fix the lookup and leave a partial upgrade
    behind, breaking the chroot instead of the build (3.2.0-B5).
    """
    if not policy.enabled or not policy.chroot_dir:
        return
    missing = missing_toolchain(policy, exports, conf_path=conf_path)
    if not missing:
        return

    unmappable = sorted(b for b, pkg in missing.items() if not pkg)
    if unmappable:
        root = policy.chroot_dir / "root"
        raise SandboxUnavailable(
            f"the sandbox chroot has no {', '.join(unmappable)} and sysforge "
            f"does not know which package provides it — install it yourself "
            f"with: arch-nspawn {root} pacman -Syu <package>"
        )

    packages = sorted({pkg for pkg in missing.values()})
    root = policy.chroot_dir / "root"
    _log.info(
        "Chroot is missing " + ", ".join(sorted(missing))
        + " — installing " + ", ".join(packages) + " into " + str(root)
    )
    try:
        run_privileged(
            ["arch-nspawn", str(root), "pacman", "-Syu", "--needed",
             "--noconfirm", *packages],
            tag="SANDBOX",
        )
    except Exception as exc:
        raise SandboxUnavailable(
            f"could not install the profile's toolchain "
            f"({', '.join(packages)}) into the sandbox chroot at {root}: "
            f"{exc} — the build would fail with a missing "
            f"{', '.join(sorted(missing))}"
        ) from exc


@contextlib.contextmanager
def as_canonical_pkgbuild(pkgbuild_path: Path):
    """Present *pkgbuild_path* as ``./PKGBUILD`` for the duration of the block.

    ``makechrootpkg`` bind-mounts the invocation directory and runs
    ``source PKGBUILD`` on the host to read pkgbase, so it cannot honour an
    arbitrary ``-p <name>``. sysforge, meanwhile, never builds the upstream
    file: every route through ``makepkg_wrapper._run_build`` reassigns the path
    to the patched ``PKGBUILD.sysforge`` sidecar. Without reconciling the two
    the sandbox refused every package it was pointed at (3.2.0-B1).

    The reconciliation is a scoped rename in the package directory rather than
    a staged copy elsewhere: the build needs everything else that directory
    holds — local source files, ``.install`` scripts, patches, keys — and
    replicating that set is a larger surface to get wrong than a swap that is
    undone in a ``finally``.

    Yields the canonical path. A no-op (yielding the original) when the file is
    already named ``PKGBUILD``. Not concurrency-safe within one package
    directory, which matches the build loop: sysforge builds a given package
    once at a time.
    """
    pkgbuild_path = Path(pkgbuild_path)
    if pkgbuild_path.name == REQUIRED_PKGBUILD_NAME:
        yield pkgbuild_path
        return

    directory = pkgbuild_path.parent
    canonical = directory / REQUIRED_PKGBUILD_NAME
    stash = directory / UPSTREAM_STASH_NAME

    # Re-checked here and not only in preflight: this is the step that would do
    # the damage, and ``rename`` replaces silently on POSIX.
    if stash.exists():
        raise SandboxUnavailable(
            f"refusing to sandbox: {stash} is left over from an interrupted "
            f"build, and swapping over it would lose the upstream PKGBUILD "
            f"— restore it with: mv {stash} {canonical}"
        )

    stashed = False
    if canonical.exists():
        canonical.rename(stash)
        stashed = True
    try:
        pkgbuild_path.rename(canonical)
    except OSError:
        # Never leave the checkout half-swapped because step two failed.
        if stashed:
            stash.rename(canonical)
        raise

    try:
        yield canonical
    finally:
        with contextlib.suppress(OSError):
            canonical.rename(pkgbuild_path)
            if stashed:
                stash.rename(canonical)


def mem_cap_applies(policy: SandboxPolicy) -> bool:
    """Whether an ``RLIMIT_AS`` child cap still binds the actual build.

    False under the sandbox: the preexec would cap ``makechrootpkg``, whose
    real work happens in an ``systemd-nspawn`` container several exec layers
    down and in its own cgroup. Reported rather than silently ignored.
    """
    return not policy.enabled


def chroot_env(env: dict) -> dict:
    """Return the environment ``makechrootpkg`` itself should be launched with.

    Only the dest variables survive its ``sudo`` re-exec (devtools' preserve
    list), and they are what makes the sandbox path drop artifacts in the same
    place the host path does. Everything else the *build* needs is delivered
    through the container-side conf instead — see :func:`chroot_conf_text`.
    """
    out = dict(env)
    # MAKEPKG_CONF is meaningless to makechrootpkg (it loads the host conf for
    # its own dest resolution and the container conf for the build) and would
    # only confuse a reader of the process table.
    out.pop("MAKEPKG_CONF", None)
    return out


def _container_buildenv(conf_path: Path) -> str | None:
    """Return a ``BUILDENV=(...)`` line with the host-only accelerators off.

    ``None`` when the conf sets no ``BUILDENV`` — makepkg's own default already
    disables both, so emitting a line would only add noise. Every other option
    keeps its position and its sense: this corrects tooling availability, not
    the user's build policy.
    """
    from sysforge.primitives.config import parse_system_makepkg_conf

    raw = (parse_system_makepkg_conf(conf_path).get("BUILDENV") or "").strip()
    if not raw:
        return None
    if raw.startswith("(") and raw.endswith(")"):
        raw = raw[1:-1]
    options = []
    for opt in raw.split():
        if opt.lstrip("!") in _HOST_ONLY_BUILDENV and not opt.startswith("!"):
            opt = f"!{opt}"
        options.append(opt)
    return "BUILDENV=(" + " ".join(options) + ")"


def chroot_conf_text(conf_path: Path, exports: dict | None = None) -> str:
    """Derive the container-side ``makepkg.conf`` from the emitted host one.

    Appends, after the original body so they win by re-assignment:

    * the container's dest paths (``BUILDDIR=/build`` &c) — the host values
      name directories that do not exist inside the container;
    * a ``BUILDENV`` with ``ccache``/``distcc`` forced off — they name host
      binaries the clean chroot does not carry (:data:`_HOST_ONLY_BUILDENV`);
    * ``export`` lines for the env-delivered keys (``CC``/``CXX`` and the
      profile's env-type keys). Those are deliberately absent from the conf on
      the host path because makepkg sources the conf *after* the invocation
      env is set; inside the container there is no invocation env to inherit,
      so the conf is the only channel left.
    """
    body = Path(conf_path).read_text()
    lines = [body.rstrip("\n"), "", "# --- sysforge build sandbox ---"]
    for key, value in _CHROOT_DEST_KEYS.items():
        lines.append(f"{key}={value}")
    buildenv = _container_buildenv(conf_path)
    if buildenv is not None:
        lines.append(buildenv)
    for key, value in sorted((exports or {}).items()):
        if key in _ENV_EXPORT_DENY or not key.isidentifier():
            continue
        lines.append(f"export {key}={shlex.quote(str(value))}")
    return "\n".join(lines) + "\n"


def dest_env_from_conf(conf_path: Path) -> dict:
    """Return the dest variables ``makechrootpkg`` must be launched with.

    Read out of the *emitted* conf rather than the system one: that file is
    this build's authority for ``PKGDEST`` & co (profile overrides included),
    and ``makechrootpkg`` cannot see it — it loads the host's own conf for its
    dest resolution, so an unexported override would relocate the artifacts
    out from under ``_find_built_packages``. Values are unquoted; keys absent
    from the conf are simply not set (``makechrootpkg`` then falls back to
    ``$PWD``, which is also where the host path leaves them).
    """
    from sysforge.primitives.config import parse_system_makepkg_conf

    out: dict[str, str] = {}
    assignments = parse_system_makepkg_conf(conf_path)
    for key in PRESERVED_DEST_ENV:
        raw = (assignments.get(key) or "").strip()
        if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
            raw = raw[1:-1]
        if raw:
            out[key] = raw
    return out


# What the container-conf audit inspects (3.4.0-F2): the keys whose values a
# tool reads at build time. Package-internal locations (DBGSRCDIR, MAN_DIRS,
# DOC_DIRS, PURGE_TARGETS, STRIP_DIRS) name paths *inside the package*, not
# the filesystem, and are deliberately not scanned. This says what to inspect,
# not what may cross — so it is not the full allowlist 3.4.0-F2 rejected.
_AUDIT_EXTRA_KEYS = ("BUILDENV",)
_EXPORT_RE = re.compile(r"^[ \t]*export[ \t]+([A-Za-z_][A-Za-z0-9_]*)=", re.MULTILINE)
_PREFIX_MAP_RE = re.compile(r"^-f(?:file|debug|macro)-prefix-map=")
# An absolute path as a whole token, after `=`, or glued to -I/-L.
_ABS_PATH_RE = re.compile(r"(?:^-[IL]|^|=)(/[^\s'\"=:,]*)")
# Prefixes a dependency installed into the working copy may legitimately
# populate after the root was cloned, so absence from root/ proves nothing.
_DEP_PREFIXES = ("/usr/", "/opt/")
# The container's own tmpfs (makechrootpkg mounts one); a classification
# literal, not a temp-file use.
_CONTAINER_TMP = "/tmp"  # noqa: S108
_BUILDENV_TOOLS = ("ccache", "distcc")


def _audit_tokens(raw: str) -> list[str]:
    value = raw.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    try:
        words = shlex.split(value, comments=True)
    except ValueError:
        words = [value]
    # A quoted scalar (CFLAGS="-O2 -fuse-ld=lld") is one shell word that the
    # consuming tool word-splits again, so split each word on whitespace too.
    return [tok for word in words for tok in word.split()]


def _container_has_path(root: Path, path: str) -> bool:
    if any(path == p or path.startswith(p.rstrip("/") + "/")
           for p in (*_CHROOT_DEST_KEYS.values(), CHROOT_STARTDIR, _CONTAINER_TMP)):
        return True
    if path.startswith(_DEP_PREFIXES):
        return True
    return (root / path.lstrip("/")).exists()


def _audit_refuse(key: str, value: str, why: str, home: str) -> None:
    raise SandboxUnavailable(
        f"refusing to sandbox: the container makepkg.conf sets {key}={value}: {why}. "
        f"Correction set that should cover it: {home}"
    )


def audit_chroot_conf(conf_text: str, policy: SandboxPolicy) -> None:
    """Refuse a final container conf that still names the host (3.4.0-F2).

    Runs on :func:`chroot_conf_text`'s output, after provisioning, so it judges
    the real ``root/`` tree. Two classifiers: an absolute path that will not
    exist in the container, and a binary in a compiler/linker/BUILDENV slot the
    chroot does not carry. Each refusal names the key, the value and the
    correction set that should have covered it, so a new member of the bug class
    (3.2.0-B2/B4/B6/B7/B10/B12) is one readable stop instead of a build error
    several layers later. 3.2.0-B5 (a stale chroot database) is outside it.
    """
    from sysforge.primitives.config import parse_makepkg_conf_text

    if not policy.enabled or not policy.chroot_dir:
        return
    root = policy.chroot_dir / "root"
    if not root.is_dir():
        return
    effective = parse_makepkg_conf_text(conf_text)
    exported = set(_EXPORT_RE.findall(conf_text))

    def _has_binary(name: str) -> bool:
        probe = (root / name.lstrip("/")) if name.startswith("/") \
            else (root / "usr" / "bin" / name)
        return probe.exists()

    for key in _TOOLCHAIN_ENV_KEYS:
        # Only exported values reach make/cmake: makepkg exports the *FLAGS
        # keys, CHOST and MAKEFLAGS and nothing else, so a plain `AR=llvm-ar`
        # in the copied host conf is inert (found against a live conf).
        if key not in exported:
            continue
        raw = effective.get(key)
        words = _audit_tokens(raw) if raw else []
        if words and "$" not in words[0] and not _has_binary(words[0]):
            home = "_TOOLCHAIN_PACKAGE (provision_toolchain)" \
                if Path(words[0]).name in _TOOLCHAIN_PACKAGE else "_ENV_EXPORT_DENY"
            _audit_refuse(key, words[0], "binary not present in the sandbox chroot", home)

    for opt in _audit_tokens(effective.get("BUILDENV", "")):
        if opt in _BUILDENV_TOOLS and not _has_binary(opt):
            _audit_refuse("BUILDENV", opt, "accelerator binary not present in the sandbox "
                          "chroot", "_HOST_ONLY_BUILDENV")

    scanned = [k for k in effective
               if k.endswith("FLAGS") or k in exported or k in _CHROOT_DEST_KEYS
               or k in _AUDIT_EXTRA_KEYS]
    for key in scanned:
        if key in _TOOLCHAIN_ENV_KEYS or key == "BUILDENV":
            continue
        for tok in _audit_tokens(effective[key]):
            if "$" in tok or _PREFIX_MAP_RE.match(tok):
                continue
            if tok.startswith("-fuse-ld="):
                name = tok.split("=", 1)[1]
                binary = name if name.startswith("/") else f"ld.{name}"
                if not _has_binary(binary):
                    _audit_refuse(key, tok, f"linker {binary} not present in the sandbox "
                                  "chroot", "_TOOLCHAIN_PACKAGE (provision_toolchain)")
                continue
            if key in exported and key.endswith("_WRAPPER") and not tok.startswith(("-", "/")):
                # A *_WRAPPER value names a binary (RUSTC_WRAPPER=sccache). Other
                # bare words are policy values (CARGO_PROFILE_RELEASE_LTO=fat).
                if not _has_binary(tok):
                    _audit_refuse(key, tok, "binary not present in the sandbox chroot",
                                  "_ENV_EXPORT_DENY")
                continue
            for path in _ABS_PATH_RE.findall(tok):
                if _container_has_path(root, path):
                    continue
                home = ("_PROFILE_USE_PREFIXES (provision_profile_data)"
                        if _PROFILE_USE_RE.search(" " + tok) else
                        "_CHROOT_DEST_KEYS" if key in _CHROOT_DEST_KEYS else
                        "_ENV_EXPORT_DENY")
                _audit_refuse(key, path, "path does not exist inside the sandbox chroot",
                              home)


def build_argv(
    policy: SandboxPolicy,
    flags: list[str],
    *,
    conf_dir_name: str,
    install_pkgs: list[Path] | None = None,
) -> list[str]:
    """Assemble the ``makechrootpkg`` argv for this build.

    Shape: ``makechrootpkg -r <chroot> [-c] [-u] [-I <pkg>]... -- <makepkg args>``.

    Three shape facts that are not obvious:

    * No ``sudo`` prefix — ``makechrootpkg`` escalates itself and would lose
      its preserved environment if wrapped (see the module docstring).
    * ``-l`` names the working copy (:data:`COPY_NAME`) so provenance can
      fingerprint the compiler that actually built the package (3.4.0-B3).
    * No ``-p`` — the container builds the ``PKGBUILD`` in the bind-mounted
      directory, which :func:`preflight` has already established is the file we
      mean.
    * ``--config`` names the conf through the *container's* view of that same
      bind mount (``/startdir/<scratch>/…``), the only path that is valid on
      both sides of the boundary.

    ``install_pkgs`` are host artifacts injected into the working copy before
    the build — the run's own already-built dependencies, which the container
    otherwise cannot see.

    The local repo (3.1.0-F10) needs no mount flag here: arch-nspawn binds
    every ``file://`` Server in the container's pacman.conf read-only.
    """
    argv = ["makechrootpkg", "-r", str(policy.chroot_dir), "-l", COPY_NAME]
    if policy.clean:
        argv.append("-c")
    if policy.update:
        argv.append("-u")
    for pkg in install_pkgs or []:
        argv += ["-I", str(pkg)]
    argv.append("--")
    argv.append(f"--config={CHROOT_STARTDIR}/{conf_dir_name}/{CHROOT_CONF_NAME}")
    # makechrootpkg already passes --syncdeps --noconfirm --log --holdver
    # --skipinteg; ours are appended after and win where they conflict.
    argv += list(flags)
    return argv


# ---------------------------------------------------------------------------
# Session artifact registry
# ---------------------------------------------------------------------------
#
# Dependencies built earlier in the same run are installed on the *host*, which
# a container cannot see; ``makechrootpkg -I`` is the only way back in. The
# registry is module-global for the same reason ``net_policy``'s is: the
# producers (the build loop, the AUR dep loop) and the consumer (the makepkg
# invocation seam) sit at very different depths, and a threaded parameter
# defaults to "no deps" at every call site that forgets it — which under the
# sandbox is a build failure rather than a silent weakening, but a needless one.


@dataclass
class _Session:
    artifacts: list[Path] = field(default_factory=list)


_session = _Session()


def register_artifacts(paths) -> None:
    """Record artifacts built this run, for injection into later sandboxes.

    Idempotent and order-preserving: a package rebuilt in the same run does not
    get a duplicate ``-I``. ``.sig`` files are skipped — pacman takes the
    package file.
    """
    for raw in paths or []:
        path = Path(raw)
        if path.name.endswith(".sig"):
            continue
        if path not in _session.artifacts:
            _session.artifacts.append(path)


def _source_built_packages(state_dir) -> set:
    """Return the pkgnames ``build_state.toml`` records as not-from-pacman.

    Keyed by **pkgname**, not pkgbase, which is what makes this the right store
    to seed from: split packages are already expanded, and ``-I`` takes package
    *files*, one per pkgname. Unreadable state degrades to "nothing is
    source-built", which costs fidelity rather than correctness.
    """
    import os

    from sysforge.primitives.build_state import BUILD_MODE_PACMAN, BuildState

    if state_dir is None:
        # Same resolution ArtifactRegistry uses, and for the same reason: this
        # is a primitive, so it cannot reach pipeline.resolve_state_dir, and a
        # caller several layers up that forgets to thread the value would
        # silently degrade injection to "nothing is source-built".
        env = os.environ.get("SYSFORGE_STATE_DIR")
        state_dir = Path(env) if env else Path("/var/lib/sysforge")

    try:
        packages = BuildState(state_dir).all_packages()
    except Exception:
        return set()
    return {
        name for name, entry in packages.items()
        if (entry or {}).get("build_mode") != BUILD_MODE_PACMAN
    }


def source_built_packages(state_dir=None) -> set:
    """Public view of :func:`_source_built_packages`, for gates outside the
    sandbox that ask the same "which pkgnames did the user build?" question
    (the pre-install ABI skew gate, 3.2.0-B20) — one reader of build_state's
    build_mode for both."""
    return _source_built_packages(state_dir)


def installed_artifact(name: str, search_dir):
    """The artifact in *search_dir* at the version the host runs, or None.

    One reader for both consumers of "which build does the host run": the
    ``-I`` closure (3.1.0-F9) and the local repo (3.1.0-F10). Never the newest
    artifact — *search_dir* is a long-lived archive of every build (3.1.0-B1).
    """
    from sysforge.primitives import pacman
    from sysforge.primitives.makepkg_artifacts import find_artifacts

    version = pacman.get_installed_version(name)
    if not version:
        return None
    match = [p for p in find_artifacts(search_dir, [name], exact_ver=version)
             if not str(p).endswith(".sig")]
    return Path(match[0]) if match else None


def resolve_dep_artifacts(pkgbuild_path, *, search_dir, state_dir=None) -> list[Path]:
    """Return locally-built artifacts for *pkgbuild_path*'s source-built deps.

    ``arch-nspawn`` gives the container its own ``pacman.conf``, so
    ``makechrootpkg --syncdeps`` resolves from the stock repos and never from
    the host's installed set. A dependency the user built from source therefore
    exists only as an installed *host* package and comes back as ``target not
    found`` — or, worse, silently resolves to an older repo version. ``-I`` is
    the only channel back in (3.1.0-F9).

    Three rules, each chosen against a failure mode:

    * **Scope** is the target's dependency closure ∩ source-built, not every
      source-built package: on a stack machine the latter is ~150 files copied
      into the working copy and installed in one ``pacman -U`` per build. The
      closure is walked over the host's local DB, because ``build_state``
      records versions but no dependency edges.
    * **Version** is the one the host actually runs, never the newest artifact
      in ``search_dir`` — that directory is a long-lived archive of every
      historical build (3.1.0-B1), and injecting its newest would hand the
      container a version the host does not have, recreating the very skew
      this removes.
    * **An unreadable dependency closure refuses** (3.2.0-B19): a missing
      PKGBUILD or a failed deps/local-DB read raises ``SandboxUnavailable``
      rather than returning ``[]``, which would read as "no source-built deps".
    * **A pruned artifact warns and continues.** The container falls back to
      the repo version, which is a build-fidelity mismatch, not a breach of
      the isolation boundary — so unlike the sandbox's own refuse-rather-than-
      downgrade rule, a missing file in an archive is not worth a hard stop.

    Skew against packages outside the injection set is the local repo's job
    (``primitives/local_repo.py``, 3.1.0-F10).
    """
    from sysforge.primitives import pacman
    from sysforge.primitives.aur_resolve import _strip_version

    if not pkgbuild_path or not search_dir:
        return []

    source_built = _source_built_packages(state_dir)
    if not source_built:
        return []

    # Fail closed from here on (3.2.0-B19). Every failure below used to return
    # [], which is indistinguishable from "this target has no source-built
    # deps": the container then silently resolved them from the stock repos —
    # a libLLVM whose exports differ from the host's under the same pkgver —
    # and the resulting mesa black-screened the desktop on reboot. A pruned
    # *artifact* still warns and continues (one dep, named); losing the whole
    # closure is not that case.
    if not Path(pkgbuild_path).is_file():
        raise SandboxUnavailable(
            f"refusing to sandbox: cannot resolve source-built deps — "
            f"{pkgbuild_path} does not exist, so the container would link "
            f"against repo versions instead of the ones this host runs"
        )
    try:
        roots = pacman.collect_builddeps([pkgbuild_path], strict=True)
        if not roots:
            return []
        # One pass over the local DB rather than a `pacman -Qi` per package: a
        # whole-system walk the other way is O(N^2) directory reads (2.6.1-B22).
        graph = pacman.get_all_package_depends()
    except Exception as exc:
        raise SandboxUnavailable(
            f"refusing to sandbox: cannot resolve source-built deps of "
            f"{pkgbuild_path} ({exc}), so the container would link against "
            f"repo versions instead of the ones this host runs"
        ) from exc

    seen: set = set()
    queue = list(roots)
    closure: list = []
    while queue:
        name = _strip_version(queue.pop())
        if not name or name in seen:
            continue
        seen.add(name)
        closure.append(name)
        queue.extend(graph.get(name, []))

    found: list[Path] = []
    for name in closure:
        if name not in source_built:
            continue
        version = pacman.get_installed_version(name)
        if not version:
            continue
        art = installed_artifact(name, search_dir)
        if art is not None:
            found.append(art)
            continue
        _log.warn(
            f"sandbox: no built artifact for {name} {version} in {search_dir} "
            f"— the container will use the repo version instead, which may "
            f"not match what this host runs"
        )
    return found


def toolchain_packages(exports: dict | None, conf_path=None) -> set:
    """Packages providing the profile's toolchain binaries, present or not.

    The same binaries :func:`missing_toolchain` probes, mapped through
    :data:`_TOOLCHAIN_PACKAGE`. Used to ``-I`` the host's own toolchain build into
    the working copy: pacman never replaces an installed package with the same
    version from another repo, so a root already holding repo clang keeps it
    unless the exact file is injected (3.1.0-F10).
    """
    names = []
    for key in _TOOLCHAIN_ENV_KEYS:
        raw = str((exports or {}).get(key) or "").strip()
        try:
            words = shlex.split(raw) if raw else []
        except ValueError:
            words = []
        if words:
            names.append(Path(words[0]).name)
    if not (exports or {}).get("CC"):
        names.append("gcc")  # makepkg's default cc
    flag_values = [(exports or {}).get(k) for k in _LINKER_FLAG_KEYS]
    flag_values += _conf_flag_values(conf_path)
    names += [Path(n).name for n in _fuse_ld_names(flag_values)]
    return {_TOOLCHAIN_PACKAGE[n] for n in names if n in _TOOLCHAIN_PACKAGE}


def install_args(pkgbuild_path=None, *, search_dir=None,
                 state_dir=None, exports=None, conf_path=None) -> list[Path]:
    """Return the artifacts to ``-I`` into the next container, newest last.

    The union of two sources, the session registry winning: packages built
    earlier in *this run* are newer than anything the persistent store points
    at. With no *pkgbuild_path* it is the registry alone, which is what call
    sites that have no target in hand get.

    Only files that still exist: an artifact cleaned out between builds is a
    stale registry entry, not a reason to fail the build.
    """
    resolved: list[Path] = []
    if pkgbuild_path is not None:
        resolved = resolve_dep_artifacts(
            pkgbuild_path, search_dir=search_dir, state_dir=state_dir)

    # 3.1.0-F10: the host's own toolchain build, closure included, so the
    # working copy compiles with it even when the root holds the same version
    # from the repos. Gated on *exports* (the caller passes it only with the
    # local repo on), and on there being anything source-built at all.
    pkgs = toolchain_packages(exports, conf_path=conf_path) if exports is not None else set()
    source_built = _source_built_packages(state_dir) if pkgs else set()
    if source_built:
        from sysforge.primitives import pacman
        graph = pacman.get_all_package_depends()
        seen: set = set()
        queue = sorted(pkgs)
        while queue:
            name = queue.pop()
            if name in seen:
                continue
            seen.add(name)
            queue.extend(graph.get(name, []))
            if name in source_built and search_dir:
                art = installed_artifact(name, search_dir)
                if art is not None and art not in resolved:
                    resolved.append(art)

    session = [p for p in _session.artifacts if p.exists()]
    out = [p for p in resolved if p.exists() and p not in session]
    out.extend(session)
    return out


def reset_session() -> None:
    """Forget every registered artifact and any pending provenance."""
    _session.artifacts.clear()
    clear_provenance()


# ---------------------------------------------------------------------------
# Build provenance (3.4.0-B3)
# ---------------------------------------------------------------------------
#
# The toolchain identity build_state records is resolved once per run from the
# host, but a sandboxed build compiles with the chroot's compiler. The seam that
# ran the build knows which; the record site, several layers up, does not — so
# the seam parks what it saw here and the record site takes it. Module-global
# for the same reason as the artifact registry above.


@dataclass(frozen=True)
class SandboxProvenance:
    """What a successful sandboxed build actually compiled with."""

    cc_name: str
    copy_identity: str


_provenance: SandboxProvenance | None = None


def set_provenance(p: SandboxProvenance) -> None:
    global _provenance
    _provenance = p


def take_provenance() -> SandboxProvenance | None:
    """Return the pending provenance and clear it — one build, one record."""
    global _provenance
    p, _provenance = _provenance, None
    return p


def clear_provenance() -> None:
    global _provenance
    _provenance = None


def copy_compiler(policy: SandboxPolicy, cc_name: str) -> Path:
    """The compiler binary in the named working copy."""
    return policy.chroot_dir / COPY_NAME / "usr" / "bin" / cc_name


def recorded_toolchain(prov: SandboxProvenance, host_variant, host_fingerprint):
    """Resolve the (variant, fingerprint) build_state should record.

    The same package file on host and in the copy has the same static identity,
    so a match keeps the host's values and toolchain drift behaves as for a host
    build. A mismatch means the container compiled with a different compiler —
    the repo one — and records a fingerprint that can never equal a host one, so
    toolchain drift reports the package.
    """
    from sysforge.primitives.build_fingerprint import static_identity

    if prov.copy_identity != "none" \
            and static_identity(_HOST_BIN_DIR / prov.cc_name) == prov.copy_identity:
        return host_variant, host_fingerprint
    variant = "stock_llvm" if "clang" in prov.cc_name else "gcc"
    return variant, f"sandbox|{prov.cc_name}|{prov.copy_identity}"
