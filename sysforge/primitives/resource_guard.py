# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
resource_guard.py — virtual address space cap for the sysforge controller process.

Sets RLIMIT_AS at startup so a runaway loop or leak in the Python controller
cannot consume all system memory. The limit is lifted for makepkg child
processes (which need whatever memory they need for real builds) via
lift_for_child(), intended as a subprocess preexec_fn.
"""
import resource
from collections.abc import Callable

# 2 GiB is far more than the Python controller ever needs. Caps runaway growth.
_CONTROLLER_LIMIT_BYTES = 2 * 1024 * 1024 * 1024

# Saved at install() time so child processes can restore the original hard limit.
_original_as_hard: int = resource.RLIM_INFINITY


def install() -> None:
    """
    Cap this process's virtual address space to _CONTROLLER_LIMIT_BYTES.

    Safe to call even if the system hard limit is already lower — we will
    never attempt to raise above the hard limit.
    """
    global _original_as_hard
    try:
        _, _original_as_hard = resource.getrlimit(resource.RLIMIT_AS)
        soft = _CONTROLLER_LIMIT_BYTES
        if _original_as_hard != resource.RLIM_INFINITY and _original_as_hard < soft:
            soft = _original_as_hard
        resource.setrlimit(resource.RLIMIT_AS, (soft, _original_as_hard))
    except (ValueError, OSError):
        pass


def lift_for_child() -> None:
    """
    Restore RLIMIT_AS to the original hard limit in a child process.

    Intended as a subprocess preexec_fn so makepkg and other build tools
    are not constrained by the sysforge controller limit.
    """
    try:
        resource.setrlimit(resource.RLIMIT_AS, (_original_as_hard, _original_as_hard))
    except (ValueError, OSError):
        pass


def make_child_preexec(rlimit_as_bytes: int | None) -> Callable[[], None]:
    """Build a subprocess ``preexec_fn`` for a makepkg child.

    The returned closure always runs :func:`lift_for_child` (restore the
    controller's RLIMIT_AS to the original hard limit); when ``rlimit_as_bytes``
    is not ``None`` it then clamps RLIMIT_AS down to that per-build ceiling
    (2.2.0-F4 ``[build] mem_limit``), never above the current hard limit. Both
    steps are best-effort: a raising syscall is swallowed so it can never abort
    the child's exec. ``rlimit_as_bytes is None`` → behaves exactly like a bare
    ``lift_for_child``.

    The cap is delivered here (not via ``build_throttle.wrapper_argv``) only when
    the build is *not* wrapped in a systemd scope — see
    ``build_throttle.resolve_child_mem_cap`` for the arbitration."""
    def _preexec() -> None:
        lift_for_child()
        if rlimit_as_bytes is None:
            return
        cap = rlimit_as_bytes
        if _original_as_hard != resource.RLIM_INFINITY and cap > _original_as_hard:
            cap = _original_as_hard
        try:
            resource.setrlimit(resource.RLIMIT_AS, (cap, cap))
        except (ValueError, OSError):
            pass

    return _preexec
