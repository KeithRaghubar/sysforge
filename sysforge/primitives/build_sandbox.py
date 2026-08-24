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
  ``-p <name>``.  :func:`preflight` refuses a non-``PKGBUILD`` filename rather
  than build the wrong file.
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
    SandboxPolicy(enabled, chroot_dir, clean, update).describe()
    resolve_sandbox(cfg) -> SandboxPolicy
    for_profile(policy, resolved_profile) -> SandboxPolicy
    set_policy(policy) / get_policy() / reset_policy() / suppressed(active)
    preflight(policy, pkgbuild_path) -> None      (raises SandboxUnavailable)
    build_argv(policy, flags, conf_dir_name, install_pkgs) -> list[str]
    dest_env_from_conf(conf_path) -> dict
    chroot_conf_text(conf_path, exports) -> str
    chroot_env(env) -> dict
    mem_cap_applies(policy) -> bool
    register_artifacts(paths) / install_args() / reset_session()
"""
from __future__ import annotations

import contextlib
import shlex
import shutil
from dataclasses import dataclass, field, replace
from pathlib import Path

from sysforge import log

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
CHROOT_CONF_NAME = ".sysforge-chroot-makepkg.conf"
CHROOT_CONF_DIR_PREFIX = ".sysforge-chroot-"
CHROOT_STARTDIR = "/startdir"

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

    def describe(self) -> str:
        if not self.enabled:
            return "off (builds run on the host)"
        bits = [f"chroot={self.chroot_dir}"]
        if self.clean:
            bits.append("clean")
        if self.update:
            bits.append("update")
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

    name = Path(pkgbuild_path).name
    if name != REQUIRED_PKGBUILD_NAME:
        raise SandboxUnavailable(
            f"cannot sandbox a build of {name!r}: makechrootpkg builds the "
            f"'{REQUIRED_PKGBUILD_NAME}' in the invocation directory and reads "
            f"pkgbase from it, so a differently-named file would build one "
            f"package and report another"
        )


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


def chroot_conf_text(conf_path: Path, exports: dict | None = None) -> str:
    """Derive the container-side ``makepkg.conf`` from the emitted host one.

    Appends, after the original body so they win by re-assignment:

    * the container's dest paths (``BUILDDIR=/build`` &c) — the host values
      name directories that do not exist inside the container;
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
    * No ``-p`` — the container builds the ``PKGBUILD`` in the bind-mounted
      directory, which :func:`preflight` has already established is the file we
      mean.
    * ``--config`` names the conf through the *container's* view of that same
      bind mount (``/startdir/<scratch>/…``), the only path that is valid on
      both sides of the boundary.

    ``install_pkgs`` are host artifacts injected into the working copy before
    the build — the run's own already-built dependencies, which the container
    otherwise cannot see.
    """
    argv = ["makechrootpkg", "-r", str(policy.chroot_dir)]
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


def install_args() -> list[Path]:
    """Return the artifacts to ``-I`` into the next container, newest last.

    Only files that still exist: an artifact cleaned out between builds is a
    stale registry entry, not a reason to fail the build.
    """
    return [p for p in _session.artifacts if p.exists()]


def reset_session() -> None:
    """Forget every registered artifact. For tests and between runs."""
    _session.artifacts.clear()
