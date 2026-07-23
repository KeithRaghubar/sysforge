# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""One-home policy for makepkg exit 13 / ``AlreadyBuilt`` (2.5.1-F2).

``AlreadyBuilt`` (raised in ``makepkg_invoke``) means PKGDEST already holds a
matching ``.pkg.tar`` and makepkg skipped the build. Every catch site routes
its "what now?" decision through :func:`resolve_already_built` instead of
interpreting the exception locally — 2.5.1-B3/B4/B5 were all divergences
between sites that each re-derived the answer (in particular the unattended
arbitration, which needs *three* signals: the caller's resolved interactive
flag, the ``--non-interactive`` override, and TTY presence).

Decide-only: callers execute the chosen action (artifact reuse, ``-f``
retry). The manifest capture in ``makepkg_wrapper``'s except-path is a
side-effect, not policy, and stays there.

Postures:

* ``"reuse"`` — the skipped build is good news; the existing artifact is the
  product (build_core batch loop, toolchain passes). Always ``REUSE``.
* ``"review-gated"`` — the skipped build broke an interactive promise (the
  kernel's in-``prepare()`` kconfig review, B5). Unattended runs proceed
  (``REUSE``); interactive runs are warned and asked: install as-built,
  rebuild with ``-f`` so the review actually runs, or abort (raises).
"""

from enum import Enum

from sysforge import log


class AlreadyBuiltAction(Enum):
    """What the caller does next. Abort is a raise, never a return — the enum
    stays two-valued so no caller can forget an abort branch."""

    REUSE = "reuse"
    REBUILD = "rebuild"


def resolve_already_built(
    posture: str,
    *,
    interactive: bool,
    non_interactive: bool = False,
    tag: str = "BUILD",
    abort_hint: str = "",
) -> AlreadyBuiltAction:
    """Decide what an ``AlreadyBuilt`` build does next.

    ``interactive`` is the caller's resolved interactivity; ``non_interactive``
    the explicit CLI/config override; TTY presence is probed here. ``tag`` sets
    the log/prompt prefix; ``abort_hint`` is caller context folded into the
    abort message.
    """
    # Late import: kernel-path tests patch these at the source module, and a
    # module-level from-import would bind before the patch applies.
    from sysforge.primitives.prompt import is_interactive, prompt_choice

    if posture == "reuse":
        return AlreadyBuiltAction.REUSE
    if posture != "review-gated":
        raise ValueError(f"unknown AlreadyBuilt posture: {posture!r}")

    _log = log.get_logger(tag)
    unattended = not interactive or bool(non_interactive) or not is_interactive()
    if unattended:
        _log.info("Kernel package already built — proceeding to audit + install")
        return AlreadyBuiltAction.REUSE
    _log.warn(
        "Kernel package already built (stale package in PKGDEST) — makepkg "
        "skipped the build, so the interactive kconfig review did NOT run."
    )
    choice = prompt_choice(
        "Install as-built (i), rebuild with -f to review the config (r), "
        "or abort (a)? [i/r/A]: ",
        choices=("i", "r", "a"),
        default="a",
        eof_default="a",
        tag=tag,
        level="WARN",
    )
    if choice == "a":
        raise RuntimeError(
            f"[{tag}] aborted: package already built"
            + (f" — {abort_hint}" if abort_hint else "")
        )
    return (
        AlreadyBuiltAction.REBUILD if choice == "r" else AlreadyBuiltAction.REUSE
    )
