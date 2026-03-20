"""
resource_guard.py — virtual address space cap for the sysforge controller process.

Sets RLIMIT_AS at startup so a runaway loop or leak in the Python controller
cannot consume all system memory. The limit is lifted for makepkg child
processes (which need whatever memory they need for real builds) via
lift_for_child(), intended as a subprocess preexec_fn.
"""
import resource

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
