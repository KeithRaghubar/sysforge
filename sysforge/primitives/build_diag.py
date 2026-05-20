"""
build_diag.py — postflight failure-log diagnostics.

On a non-zero ``makepkg`` exit, scan the captured output + any side-car
build logs (meson, cargo) under the build directory for known failure
signatures and surface an actionable fix block alongside the existing
``[build_failed]`` banner.

This is the long-tail companion to
:mod:`sysforge.primitives.toolchain_preflight`: preflight catches the cases
sysforge can predict from makedepends + pkgname; postflight catches the
ones it can't (e.g. a vendored subproject pulling in rust at meson-time
without listing rust in the parent PKGBUILD's makedepends, which is exactly
how ``lib32-gstreamer`` slips past inference today).

Public API:
    diagnose(captured_lines, build_dir, *, active_rust_toolchain=None)
        -> list[FixSuggestion]
    render_suggestions(suggestions) -> str

The matcher set is deliberately small and conservative — a false positive
that contradicts the real root cause is worse than no hint at all.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_TAG = "DIAG"

# Cap how many bytes of any side-car log we slurp. The error of interest is
# always near the bottom; reading 64 KiB from the tail is more than enough
# for every signature in the table without making this loop expensive when
# pointed at a 200 MB build.log.
_LOG_TAIL_BYTES = 64 * 1024
_MESON_LOG_GLOBS = ("build*/meson-logs/meson-log.txt", "meson-logs/meson-log.txt")


@dataclass(frozen=True)
class FixSuggestion:
    signature: str       # short matcher id, e.g. "rust:E0463"
    message: str         # one-line human description of what went wrong
    fix_cmd: str | None  # exact remediation, when there is one


# ---------------------------------------------------------------------------
# Side-car log collection
# ---------------------------------------------------------------------------

def _read_tail(path: Path) -> str:
    try:
        st = path.stat()
    except OSError:
        return ""
    try:
        with path.open("rb") as f:
            if st.st_size > _LOG_TAIL_BYTES:
                f.seek(-_LOG_TAIL_BYTES, 2)
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _collect_text(captured_lines: list[str], build_dir: Path | None) -> str:
    """Concatenate the captured stdout/stderr with side-car log tails."""
    chunks: list[str] = []
    if captured_lines:
        chunks.append("\n".join(captured_lines))
    if build_dir is not None:
        for sub in build_dir.iterdir() if build_dir.is_dir() else ():
            if not sub.is_dir():
                continue
            for pat in _MESON_LOG_GLOBS:
                for hit in sub.glob(pat):
                    text = _read_tail(hit)
                    if text:
                        chunks.append(f"# {hit}\n{text}")
    return "\n".join(chunks)


# ---------------------------------------------------------------------------
# Signature matchers
# ---------------------------------------------------------------------------

_RE_E0463 = re.compile(
    r"error\[E0463\]: can't find crate for [`']std[`']"
)
_RE_E0463_TARGET = re.compile(
    r"the [`']([^`']+)[`'] target may not be installed"
)
_RE_PTP = re.compile(r"PTP not supported without Rust compiler")
_RE_MESON_UNKNOWN_OPT = re.compile(
    r"meson\.build:\d+:\d+: ERROR: Unknown options:"
)


def _suggest_rustup_target(target: str, active: str | None) -> str:
    if active:
        return f"rustup target add --toolchain {active} {target}"
    return f"rustup target add {target}"


def _match_rust_missing_std(text: str, active: str | None) -> FixSuggestion | None:
    if not _RE_E0463.search(text):
        return None
    m = _RE_E0463_TARGET.search(text)
    target = m.group(1) if m else None
    if target:
        return FixSuggestion(
            signature="rust:E0463",
            message=(
                f"rust std crate missing for target {target} — "
                "the active rust toolchain has no std for this target"
            ),
            fix_cmd=_suggest_rustup_target(target, active),
        )
    return FixSuggestion(
        signature="rust:E0463",
        message="rust std crate missing — the active rust toolchain can't find std",
        fix_cmd="rustup target list --installed   # check which targets the active toolchain has",
    )


def _match_gst_ptp(text: str, active: str | None) -> FixSuggestion | None:
    if not _RE_PTP.search(text):
        return None
    # The PTP error is downstream of either a missing rustc or a missing
    # cross target. If we also see E0463, the rust-missing-std matcher will
    # have already produced the precise fix — suppress this one to avoid
    # duplicate noise.
    if _RE_E0463.search(text):
        return None
    return FixSuggestion(
        signature="gst:ptp-no-rust",
        message=(
            "gstreamer's PTP helper requires a rust compiler for the host arch; "
            "for lib32-* this is the i686-unknown-linux-gnu target"
        ),
        fix_cmd=_suggest_rustup_target("i686-unknown-linux-gnu", active),
    )


def _match_meson_unknown_opts(text: str, active: str | None) -> FixSuggestion | None:
    del active
    if not _RE_MESON_UNKNOWN_OPT.search(text):
        return None
    return FixSuggestion(
        signature="meson:unknown-options",
        message=(
            "meson rejected an option that used to exist — likely a stale "
            "build/ directory from a previous version of the project"
        ),
        fix_cmd="rm -rf src/build  # then re-run the build",
    )


_MATCHERS = (_match_rust_missing_std, _match_gst_ptp, _match_meson_unknown_opts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def diagnose(
    captured_lines: list[str],
    build_dir: Path | None,
    *,
    active_rust_toolchain: str | None = None,
) -> list[FixSuggestion]:
    """Scan captured output + side-car logs; return any matched suggestions.

    Order is matcher-table order, deduped on ``signature`` so the same
    failure pattern never appears twice (e.g. when both stdout and the
    meson log contain the same E0463 block).
    """
    text = _collect_text(captured_lines, build_dir)
    if not text:
        return []
    out: list[FixSuggestion] = []
    seen: set[str] = set()
    for matcher in _MATCHERS:
        s = matcher(text, active_rust_toolchain)
        if s is None or s.signature in seen:
            continue
        seen.add(s.signature)
        out.append(s)
    return out


def render_suggestions(suggestions: list[FixSuggestion]) -> str:
    """Render the suggestions as a fix block, or '' when empty."""
    if not suggestions:
        return ""
    header = f"  [{_TAG}]" + " " * max(1, 17 - len(_TAG) - 2)
    lines = [f"{header}possible fixes:"]
    for s in suggestions:
        lines.append(f"    - {s.message}")
        if s.fix_cmd:
            lines.append(f"      $ {s.fix_cmd}")
    return "\n".join(lines)
