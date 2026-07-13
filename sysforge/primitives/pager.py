# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
pager.py — pipe ``print()`` through ``$PAGER`` when stdout is a TTY.

Shared by the verbs that emit potentially long output (``state list``,
``state orphans``, ``log``). Honors ``$PAGER`` if set (parsed as a shell
word list, so ``PAGER="less -RF"`` works); otherwise tries ``less -RF``
then ``more``. Degrades to passthrough when stdout isn't a TTY (CI,
redirect, pipe), when the caller passes ``use_pager=False``, or when no
pager binary can be spawned.

Public API:
    maybe_pager(use_pager) -> context manager
"""
import os
import shlex
import subprocess
import sys
from contextlib import contextmanager


def _strip_altscreen_hostile(tokens: list[str]) -> list[str]:
    """Drop ``less``'s alt-screen-hostile options (``-X`` / ``--no-init``).

    ``-X`` (long spelling ``--no-init``) tells less not to emit the terminal's
    alternate-screen init/deinit strings, so it paints inline into scrollback
    and its redraws desync on modern terminals (the B5 mangling). It can arrive
    as a standalone ``-X``, the long ``--no-init``, or smuggled inside a combined
    short-flag cluster (``-RFX``). Strip all three forms; a cluster that becomes
    a bare ``-`` after removal is dropped entirely.
    """
    out: list[str] = []
    for tok in tokens:
        if tok in ("-X", "--no-init"):
            continue
        if tok.startswith("-") and not tok.startswith("--") and "X" in tok:
            stripped = tok.replace("X", "")
            if stripped != "-":
                out.append(stripped)
            continue
        out.append(tok)
    return out


def _sanitize_less_value(value: str) -> str:
    """Sanitize a ``$LESS`` option string, removing ``-X`` / ``--no-init``.

    ``less`` reads ``$LESS`` for default options *automatically*, so an
    inherited ``LESS=-RFX`` reintroduces the B5 alt-screen suppression even when
    the argv is the built-in ``less -RF`` — hence the seam must neutralize the
    env var too, not just the ``$PAGER`` argv. Round-trips through
    :func:`shlex.split` so combined and space-separated forms both normalize.
    """
    return " ".join(_strip_altscreen_hostile(shlex.split(value)))


def _pager_candidates() -> list[list[str]]:
    """Ordered argv candidates to try, most-preferred first.

    ``$PAGER`` (if set) is parsed with :func:`shlex.split` so a value that
    carries its own flags — ``PAGER="less -RF"`` — becomes a real argv rather
    than a single un-spawnable token. When that argv is a ``less`` invocation
    its alt-screen-hostile flags are stripped (:func:`_strip_altscreen_hostile`)
    so an inherited ``PAGER="less -RFX"`` can't reintroduce the B5 mangling on
    the ``--interactive`` review path (which skips ``_suppress_pagers_in_env``).
    Foreign pagers are left untouched — only ``less`` reads ``-X`` this way. The
    built-in fallbacks follow, so a mis-set or missing ``$PAGER`` still degrades
    to a working pager.

    The ``less`` fallback is ``-RF`` — **never** ``-X`` (see above). ``-R``
    passes ANSI colour through; ``-F`` skips the pager entirely when the output
    fits one screen.
    """
    candidates: list[list[str]] = []
    pager_env = os.environ.get("PAGER")
    if pager_env:
        parts = shlex.split(pager_env)
        if parts:
            if os.path.basename(parts[0]) == "less":
                parts = [parts[0], *_strip_altscreen_hostile(parts[1:])]
            candidates.append(parts)
    candidates.append(["less", "-RF"])
    candidates.append(["more"])
    return candidates


def _sanitized_pager_env() -> dict[str, str] | None:
    """Return an environment for the pager subprocess with ``$LESS`` sanitized.

    ``None`` when ``$LESS`` is unset or already clean, so the common case still
    inherits the parent environment untouched (``Popen(env=None)``). See
    :func:`_sanitize_less_value` for why the env var — not just the argv — has
    to be neutralized on the ``--interactive`` path.
    """
    less = os.environ.get("LESS")
    if less is None:
        return None
    sanitized = _sanitize_less_value(less)
    if sanitized == less:
        return None
    env = dict(os.environ)
    env["LESS"] = sanitized
    return env


@contextmanager
def maybe_pager(use_pager: bool):
    """Pipe ``print()`` through ``$PAGER`` when stdout is a TTY and the user
    hasn't disabled paging.

    Falls back to a passthrough write when stdout isn't a TTY (CI, redirect),
    when ``use_pager`` is False, or when the pager binary can't be spawned.
    Honors ``$PAGER`` if set; otherwise tries ``less -RF`` then ``more`` (see
    :func:`_pager_candidates`).
    """
    if not use_pager or not sys.stdout.isatty():
        yield
        return
    # A verb may still hold an active ui.progress scroll region (DECSTBM
    # ``ESC[1;N-1r``) when it reaches its paged output — `state orphans`
    # paints a "starting…" phase before scanning, then pages the result. A
    # pager launched *inside* that region is clamped to ``[1, N-1]``: less
    # can't own the bottom row, so its alternate-screen redraws desync into
    # the blank-open / scroll-up-only / looping-top corruption (B5). Release
    # the region for the pager's lifetime — the same contract `suspended()`
    # already provides for makepkg's TTY-inheriting child. Writes to stderr,
    # so it's unaffected by the stdout swap below; no-op outside TTY mode.
    from sysforge.ui import progress

    pager_env = _sanitized_pager_env()
    for cmd in _pager_candidates():
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, text=True, env=pager_env)
        except (FileNotFoundError, OSError):
            continue
        old_stdout = sys.stdout
        with progress.suspended():
            try:
                sys.stdout = proc.stdin  # type: ignore[assignment]
                yield
            finally:
                sys.stdout = old_stdout
                try:
                    if proc.stdin and not proc.stdin.closed:
                        proc.stdin.close()
                except BrokenPipeError:
                    pass
                proc.wait()
        return
    yield
