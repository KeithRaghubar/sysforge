# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
makepkg_invoke.py — makepkg subprocess invocation + retry

Runs ``makepkg`` under a pty (so cargo/ninja live progress survives), classifies
its failures (clang-flag-rejected-by-gcc → ToolchainMismatchError; already-built
→ AlreadyBuilt), and wraps the call in a sudo-timeout retry that can re-auth and
hand the already-built artifact to ``pacman -U`` instead of rebuilding.  Owns the
``[MAKEPKG]`` tag.

All narration here — the inherited-shell-env scrub, build-status lines, the
toolchain-mismatch detection, and the retry prompts — is emitted under
``[MAKEPKG]``: every one describes this module's single job of launching
makepkg correctly and classifying its outcome (P2b.6b collapse).  The pure
flag transforms and env resolvers it draws on stay in ``makepkg_flags`` /
``makepkg_env``, which keep their own ``[FLAG]`` / ``[ENV]`` tags.
``invoke_makepkg`` / ``_invoke_with_retry`` / ``_build_failed_error`` and the
``ToolchainMismatchError`` / ``AlreadyBuilt`` exceptions are re-exported from
``makepkg_wrapper``.
"""
import contextvars
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from sysforge import log
from sysforge.primitives.build_throttle import (
    resolve_child_mem_cap,
    resolve_throttle,
    wrapper_argv,
)
from sysforge.primitives.editor import editor_usable, resolve_editor, run_tty_argv
from sysforge.primitives.makepkg_artifacts import _find_built_packages
from sysforge.primitives.makepkg_env import (
    _effective_build_dir,
    _logdest_tail,
    resolve_build_python,
)
from sysforge.primitives.makepkg_flags import (
    INSTALL_FLAGS,
    resolve_effective_linker,
)
from sysforge.primitives.profile import CONF_KEY_MAP
from sysforge.primitives.prompt import prompt_choice, prompt_text
from sysforge.primitives.resource_guard import make_child_preexec
from sysforge.primitives.pty_runner import run_with_pty, strip_ansi

_makepkg_log = log.get_logger("MAKEPKG")


# Heartbeat cadence for invoke_makepkg's pty loop. Ninja attached to a pty
# uses \r to redraw a single status line in place and rarely emits \n during
# a long compile phase, so without a heartbeat both the terminal (under -vvv)
# and the per-package log stay silent for many minutes even though the build
# is healthy. The pty runner uses this to surface the latest \r-overwritten
# segment (or a "no output" marker) at this cadence.
MAKEPKG_HEARTBEAT_S = 30.0

# On a build failure at normal verbosity the live stream only reaches the
# terminal (forward_bytes) — the per-package log gets heartbeats, not the real
# error. Persist this many trailing lines of captured output to the log on
# failure so the failing block (e.g. ninja's `FAILED:` lines) is diagnosable
# after the fact without a manual re-run.
MAKEPKG_FAILURE_TAIL_LINES = 80

# Substrings in makepkg output that identify a clang-only flag rejected by
# GCC. When any of these appears on stderr/stdout alongside a non-zero exit,
# invoke_makepkg raises ToolchainMismatchError so _run_build can retry once
# with the GCC flag guard forced on.
TOOLCHAIN_MISMATCH_PATTERNS = (
    "unrecognized argument to '-flto=' option",
    "unrecognized command-line option '-flto=thin'",
)

class ToolchainMismatchError(subprocess.CalledProcessError):
    """
    Raised by invoke_makepkg when the makepkg failure was caused by the
    active profile's compiler flags being incompatible with the actual
    compiler the package's build system invoked — most commonly, clang-only
    flags like -flto=thin fed to a hardcoded g++.

    Distinct from CalledProcessError so _run_build can catch it specifically
    and trigger an automatic retry with the GCC+LTO flag guard forced on,
    without prompting the user for manual correction.
    """

class AlreadyBuilt(Exception):
    """
    Raised by invoke_makepkg when makepkg refuses to rebuild because PKGDEST
    already holds a .pkg.tar matching the current pkgname-pkgver-pkgrel-arch
    (exit code 13 = E_ALREADY_BUILT, or the matching diagnostic line).
    Callers should locate the existing artifact and install it rather than
    treat this as a build failure.
    """
    def __init__(self, pkgbuild_path: Path):
        self.pkgbuild_path = pkgbuild_path
        super().__init__(f"package already built: {pkgbuild_path}")

def invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                   extra_env=None, extra_flags=None, interactive=False,
                   strip_flags=None):
    pkgbuild_path = Path(pkgbuild_path).resolve()
    build_dir = pkgbuild_path.parent

    env = os.environ.copy()

    # Sysforge's venv is stripped from os.environ once at CLI startup by
    # cli._strip_venv_from_path(), so PATH here is already clean. Pop
    # VIRTUAL_ENV / PYTHONPATH defensively in case extra_env (built before the
    # startup strip, or via an alternate entry point) carries them.
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)

    # Strip all makepkg-managed and toolchain keys from the inherited shell env
    # so the temp conf and profile env injection are the sole authority.
    # Without this, shell vars like CC=clang or CFLAGS=... win over what the
    # conf/profile sets, producing unpredictable builds.
    _strip_keys = CONF_KEY_MAP.get("makepkg", set()) | CONF_KEY_MAP.get("toolchain", set())
    for k in sorted(_strip_keys):
        if k in env:
            _makepkg_log.info(
                f"Stripped from shell env (superseded by profile): {k}={env.pop(k)!r}"
            )

    # LLVM_PROFILE_FILE is only meaningful during PGO pass 2, where the
    # toolchain stage injects it via extra_env.  If inherited from the shell
    # (e.g. user .zprofile), it causes every clang invocation to emit profraw
    # files into an unrelated directory.
    _llvm_pf = env.pop("LLVM_PROFILE_FILE", None)
    if _llvm_pf:
        _makepkg_log.info(
            f"Stripped inherited LLVM_PROFILE_FILE={_llvm_pf!r} (only set during PGO pass 2)"
        )

    env["MAKEPKG_CONF"] = str(conf_path)

    # Suppress pagers for any subprocess run by PKGBUILDs. libinput-git's
    # meson summary and git log in prepare() both pipe through less(1) and
    # stall unattended batch builds waiting for the user to quit. Override
    # unconditionally — an exported PAGER=less from the user's shell is a
    # preference for interactive shells, not consent to page mid-build.
    if not interactive:
        env["GIT_PAGER"] = "cat"
        env["PAGER"] = "cat"
        env["SYSTEMD_PAGER"] = "cat"
        env["LESS"] = "-RFX"

    if extra_env:
        for k, v in sorted(extra_env.items()):
            if k in env:
                _makepkg_log.warn(f"Overriding shell {k}={env[k]!r} with profile value {v!r}")
        env.update(extra_env)

    # Pin the configured build python ahead of any interpreter version-manager
    # shim (pyenv/asdf/conda) on the inherited PATH, so a bare ``python`` in a
    # PKGBUILD's build() resolves to the interpreter its ``python-*``
    # makedepends were installed against (the system python by default) rather
    # than a shim that lacks them. Default ([build] python unset) = the system
    # python; the choice is logged at DEBUG by ``resolve_build_python``.
    _build_python = resolve_build_python()
    if _build_python is not None:
        env["PATH"] = str(_build_python.parent) + os.pathsep + env.get("PATH", "")

    flags = list(resolved_profile.get("makepkg_flags", []))
    if interactive:
        flags = [f for f in flags if f != "--noconfirm"]
        _makepkg_log.info("--interactive: stripped --noconfirm from profile flags")
    if extra_flags:
        flags += extra_flags
        _makepkg_log.info(f"Appending CLI flags: {extra_flags}")
    if strip_flags:
        before = flags[:]
        flags = [f for f in flags if f not in strip_flags]
        removed = [f for f in before if f not in flags]
        if removed:
            _makepkg_log.info(f"Batch mode: stripped flags {removed}")
    # Throttle prefix: nice/ionice front-ends or a systemd-run --scope carrying
    # a CPUQuota ceiling, resolved from [build] + the profile. Best-effort —
    # wrapper_argv drops any piece whose tool is missing rather than failing the
    # build (see build_throttle). The systemd-run scope keeps the controlling
    # TTY, so the interactive Popen path below still gets its prompts.
    throttle = resolve_throttle(resolved_profile)
    prefix = wrapper_argv(throttle)
    if prefix:
        _makepkg_log.info(f"Throttling build: {' '.join(prefix)}")
    # Per-build memory ceiling ([build] mem_limit, 2.2.0-F4). On the systemd-run
    # --scope path wrapper_argv already carried it as MemoryMax, so
    # resolve_child_mem_cap returns None here and the child preexec applies no
    # rlimit; off that path it returns the byte cap for RLIMIT_AS. make_child_preexec
    # always runs lift_for_child first, so the None case is exactly today's behaviour.
    child_mem_cap = resolve_child_mem_cap(throttle)
    if child_mem_cap is not None:
        _makepkg_log.info(f"Capping build memory (RLIMIT_AS): {child_mem_cap} bytes")
    cmd = prefix + ["makepkg", "-p", pkgbuild_path.name] + flags

    _makepkg_log.info(f"Running {' '.join(cmd)} in {build_dir} with MAKEPKG_CONF={conf_path}")

    # Interactive branch: inherit the parent's stdio so unbuffered prompts
    # (pacman conflict, sudo password, gpg signing key) reach the terminal
    # before the user is asked to type. Trades the line-classification path
    # (toolchain-mismatch retry, stdout-match AlreadyBuilt, missing-dep
    # prettification, captured_output for auto_repair) for prompt visibility.
    # Exit-code signals (13 → AlreadyBuilt, 8 → install failure) still fire.
    if interactive:
        proc = subprocess.Popen(
            cmd, cwd=build_dir, env=env,
            preexec_fn=make_child_preexec(child_mem_cap),
        )
        returncode = proc.wait()
        if returncode != 0:
            if returncode == 13:
                raise AlreadyBuilt(pkgbuild_path)
            if returncode == 8:
                _makepkg_log.error("Dependency resolution failed.")
            cpe = subprocess.CalledProcessError(returncode, "makepkg")
            # Interactive mode inherits the TTY, so makepkg's stdout was never
            # captured. Recover a diagnosis from the build's side-car logs
            # (meson-log.txt / CMakeError.log) under the effective build dir so
            # `sysforge state failed` records a real signature instead of just
            # "Aborted by user". Best-effort — never mask the real failure.
            try:
                from sysforge.primitives.build_diag import (
                    diagnose as _build_diagnose,
                    render_suggestions as _render_diag_suggestions,
                )
                diag_dir = _effective_build_dir(pkgbuild_path, resolved_profile)
                # Interactive stdout was never captured; recover the build
                # output from the LOGDEST log (if OPTIONS+=log) so the matchers
                # have text beyond the meson/cmake side-cars.
                _log_lines = _logdest_tail(pkgbuild_path)
                _suggestions = _build_diagnose(_log_lines, diag_dir)
                if _suggestions:
                    _makepkg_log.info(_render_diag_suggestions(_suggestions))
                    # Deliberate dynamic attr, read via getattr by callers.
                    cpe.diagnosis = _suggestions  # pyright: ignore[reportAttributeAccessIssue]
            except Exception as _diag_e:
                _makepkg_log.debug(f"interactive postflight diagnosis skipped: {_diag_e}")
            # Deliberate dynamic attr, read via getattr by callers.
            cpe.captured_output = []  # pyright: ignore[reportAttributeAccessIssue]
            raise cpe
        return

    # Non-interactive branch: attach makepkg's stdout+stderr to a pty so child
    # tools that gate live UI on isatty() (cargo, configure spinners) emit
    # their progress animation. run_with_pty forwards bytes verbatim to
    # sys.stdout when sysforge itself is on a tty (so the user sees the live
    # animation), and delivers decoded lines to _on_line regardless. The
    # callback runs the same failure classification (prepare/build/package),
    # missing-dep collection, already-built detection, and clang->GCC mismatch
    # pattern matching (with curly-quote normalization) as before. In verbose
    # mode, byte forwarding is suppressed and lines route to _makepkg_log
    # (prefixed [MAKEPKG][DEBUG] in the log file). When sysforge stdout is
    # piped (e.g. `sysforge update | tee log.txt`), byte forwarding is also
    # suppressed so captured logs stay free of \r/ANSI garbage.
    verbose_log = log.get_verbosity() >= 3
    failed_stage = None
    missing_deps: list[str] = []
    toolchain_mismatch = False
    already_built = False
    captured_lines: list[str] = []

    def _on_line(stripped: str) -> None:
        nonlocal failed_stage, toolchain_mismatch, already_built
        # The pty makes the child think it has a terminal, so compilers embed
        # color/erase/OSC-8-hyperlink escapes *inside* diagnostic tokens (GCC
        # hyperlinks the quoted option name). Strip them before any substring
        # match, capture, or logging — raw bytes still reach the terminal via
        # forward_bytes.
        stripped = strip_ansi(stripped)
        captured_lines.append(stripped)
        if "A failure occurred in prepare()." in stripped:
            failed_stage = "prepare"
        elif "A failure occurred in build()." in stripped:
            failed_stage = "build"
        elif "A failure occurred in package()." in stripped:
            failed_stage = "package"
        elif "target not found:" in stripped:
            missing_deps.append(stripped.strip())
        elif "A package has already been built" in stripped:
            already_built = True
        if not toolchain_mismatch:
            # GCC emits curly quotes ‘…’ (U+2018/U+2019) when the locale
            # supports them; normalize to ASCII apostrophes so our patterns
            # match regardless of the shell's LC_MESSAGES setting.
            _normalized = stripped.replace("\u2018", "'").replace("\u2019", "'")
            for _pat in TOOLCHAIN_MISMATCH_PATTERNS:
                if _pat in _normalized:
                    toolchain_mismatch = True
                    break
        if verbose_log:
            _makepkg_log.debug(stripped)

    def _on_idle(latest: str | None) -> None:
        # Surfaces ninja-style \r status redraws (and pure silence) into the
        # log so `sysforge log <pkg>` keeps moving during long compile phases.
        # Routed through _makepkg_log so the entry inherits the same
        # [SYSFORGE][DEBUG][MAKEPKG] prefix and ends up in the per-package log
        # automatically — visible at -vvv on the terminal, always in the file.
        if latest:
            _makepkg_log.debug(f"[heartbeat] {strip_ansi(latest)}")
        else:
            _makepkg_log.debug(f"[heartbeat] no output for ~{MAKEPKG_HEARTBEAT_S:.0f}s")

    forward_bytes = (not verbose_log) and sys.stdout.isatty()
    # makepkg's child tools (cargo/ninja/cmake) do their own full-screen cursor
    # addressing. Size the child's pty one row shorter than the terminal (the
    # rows the progress bar reserves) so the child confines its redraws/scrolling
    # to the region above the bar and never collapses output onto the bar row —
    # the bar stays permanently visible during the build.
    from sysforge.ui import progress
    returncode = run_with_pty(
        cmd, cwd=build_dir, env=env,
        line_callback=_on_line,
        forward_bytes=forward_bytes,
        preexec_fn=make_child_preexec(child_mem_cap),
        idle_callback=_on_idle,
        idle_timeout_s=MAKEPKG_HEARTBEAT_S,
        reserve_bottom_rows=progress.reserved_rows(),
    )

    if returncode != 0:
        # Persist the tail of real makepkg/ninja output to the per-package log.
        # At normal verbosity the live stream only reaches the terminal
        # (forward_bytes) and the log gets heartbeats, so without this the actual
        # failing block (e.g. ninja's `FAILED:` lines) never lands on disk and the
        # failure can't be diagnosed after the fact. Logged at DEBUG so it goes to
        # the per-package log file without re-spamming the console; at -vvv every
        # line was already logged, so skip. Exit 13 (already-built) is not a real
        # failure — no tail needed there.
        if not verbose_log and returncode != 13 and not already_built and captured_lines:
            tail = captured_lines[-MAKEPKG_FAILURE_TAIL_LINES:]
            _makepkg_log.debug(
                f"--- last {len(tail)} line(s) of makepkg output "
                f"(build failed, exit {returncode}) ---"
            )
            for _line in tail:
                _makepkg_log.debug(_line)
            _makepkg_log.debug("--- end captured makepkg output ---")
        # Exit code 13 = E_ALREADY_BUILT (matching .pkg.tar already in PKGDEST).
        # Also detect via output match for chroot wrappers that may rewrite the
        # exit code. Caller (update.py) installs the existing artifact instead
        # of treating this as a build failure.
        if returncode == 13 or already_built:
            raise AlreadyBuilt(pkgbuild_path)
        # Exit code 8 = E_INSTALL_FAILED (pacman failed to install deps).
        # Also triggered when we collected explicit "target not found" lines.
        if returncode == 8 or missing_deps:
            _makepkg_log.error("Dependency resolution failed.")
            for dep in missing_deps:
                _makepkg_log.error(f"  {dep}")
            _makepkg_log.warn(
                "This usually means related PKGBUILDs are at different versions. "
                "Run 'git pull --rebase' in each package directory to sync them, "
                "then retry with -m '-f' to force a rebuild.")
        elif failed_stage == "prepare":
            _makepkg_log.info("prepare() failed — likely an upstream issue "
                      "(patch conflict, changed upstream state, or fetch error); "
                      "sysforge does not modify prepare()")
        elif failed_stage == "build":
            _makepkg_log.info("build() failed — could be upstream or a flag/toolchain "
                      "incompatibility from the active sysforge profile")
        elif failed_stage == "package":
            _makepkg_log.info("package() failed — likely an upstream issue; "
                      "sysforge does not modify package()")
        else:
            _makepkg_log.info("see the captured output tail in the per-package log "
                      "for the failing block (or re-run with -vvv for full output)")
        if toolchain_mismatch:
            _makepkg_log.warn(
                "Detected clang-only compiler flag rejected by GCC — "
                "the package's build system likely invokes a hardcoded gcc/g++"
            )
            err = ToolchainMismatchError(returncode, "makepkg")
            # Deliberate dynamic attr, read via getattr by callers.
            err.captured_output = captured_lines  # pyright: ignore[reportAttributeAccessIssue]
            raise err

        # Postflight diagnosis — scan captured output + side-car logs (meson,
        # cargo) for known signatures and print an actionable fix block
        # before the build-failure exception propagates. This is the long
        # tail of toolchain_preflight: catches cases that aren't predictable
        # from makedepends (e.g. vendored meson subprojects that pull in
        # rust without listing it in the parent PKGBUILD).
        _suggestions = []
        try:
            from sysforge.primitives.build_diag import (
                diagnose as _build_diagnose,
                render_suggestions as _render_diag_suggestions,
            )
            _suggestions = _build_diagnose(
                captured_lines, build_dir,
                active_rust_toolchain=os.environ.get("RUSTUP_TOOLCHAIN"),
            )
            if _suggestions:
                _makepkg_log.info(_render_diag_suggestions(_suggestions))
        except Exception as _diag_e:  # diagnosis must never mask the real failure
            _makepkg_log.debug(f"postflight diagnosis skipped: {_diag_e}")

        cpe = subprocess.CalledProcessError(returncode, "makepkg")
        # consumed by auto_repair; deliberate dynamic attr, read via getattr by callers
        cpe.captured_output = captured_lines  # pyright: ignore[reportAttributeAccessIssue]
        # Postflight diagnosis (build_diag suggestions) — carried up so the
        # caller (sysforge update) can persist signature/fix_cmd alongside the
        # failure in build_state.toml. List of FixSuggestion; [] when nothing
        # matched. Deliberate dynamic attr, read via getattr by callers.
        cpe.diagnosis = _suggestions  # pyright: ignore[reportAttributeAccessIssue]
        raise cpe

def _build_failed_error(cause: Exception, message: str | None = None) -> RuntimeError:
    """Wrap a build failure in the ``[build_failed]`` RuntimeError, preserving
    the postflight ``diagnosis`` and ``captured_output`` from the underlying
    CalledProcessError so `sysforge update` can persist them to build_state.

    ``message`` overrides the default ``[build_failed] <cause>`` text (used by
    the interactive user-abort raises, which keep their own wording but still
    carry the diagnosis recovered from the build's side-car logs)."""
    err = RuntimeError(message or f"[build_failed] {cause}")
    # Deliberate dynamic attrs, mirroring the CalledProcessError/
    # ToolchainMismatchError carriers above; read via getattr by callers.
    err.diagnosis = getattr(cause, "diagnosis", None)  # pyright: ignore[reportAttributeAccessIssue]
    err.captured_output = getattr(  # pyright: ignore[reportAttributeAccessIssue]
        cause, "captured_output", None
    )
    return err

@dataclass
class RecoveryOutcome:
    action: str                      # "retry" | "abort"
    overrides: dict | None = None    # {"cc","cxx","ld"} after a successful swap


_LAST_RECOVERY: "contextvars.ContextVar[RecoveryOutcome | None]" = \
    contextvars.ContextVar("_LAST_RECOVERY", default=None)


def take_last_recovery() -> "RecoveryOutcome | None":
    """Consume the most recent RecoveryOutcome (set by the recovery menu when a
    swap succeeded). Returns None and resets after read."""
    out = _LAST_RECOVERY.get()
    _LAST_RECOVERY.set(None)
    return out


def _recover_menu_choices(have_swap: bool,
                          label: str) -> tuple[str, tuple[str, ...]]:
    swap_line = "  [c] retry with a different compiler / linker\n" if have_swap else ""
    msg = (
        f"Recover {label}:\n"
        "  [e] edit PKGBUILD in $EDITOR — retries automatically on exit\n"
        f"{swap_line}"
        "  [r] retry as-is            (Enter)\n"
        "  [a] abort\n"
        "Choice: "
    )
    choices = ("e", "c", "r", "a") if have_swap else ("e", "r", "a")
    return msg, choices


def _summary_linker(resolved_profile, conf_path):
    """The linker the failed build actually used, for the failure summary.

    Reuses the single ``resolve_effective_linker`` authority (CLAUDE.md
    one-home invariant) over the profile's LDFLAGS and the system makepkg.conf
    LDFLAGS, so a conf-level ``-fuse-ld=`` swap (e.g. the clang config's lld)
    is surfaced just like a profile-level one. Defensive: an unreadable or
    missing conf simply contributes no LDFLAGS, and the resolver falls back to
    ``"ld"`` (1.2.0-B4)."""
    system_ldflags = ""
    try:
        from sysforge.primitives.config import _parse_one_makepkg_conf
        system_ldflags = _parse_one_makepkg_conf(Path(conf_path)).get("LDFLAGS", "")
    except Exception:
        system_ldflags = ""
    return resolve_effective_linker(
        ld_override=None,
        profile_ldflags=resolved_profile.get("LDFLAGS", ""),
        system_ldflags=system_ldflags,
    )


# Coherent (cc, cxx) toolchain units. The recovery-menu swap offers these as a
# unit — pick "gcc" or "clang", never two independent free-text compilers — so a
# retry can't end up with a mismatched cc/cxx (e.g. gcc + clang++), which
# produces an incoherent override that just fails the retry confusingly
# (2.1.0-B1). Linker choice stays a separate axis (see _prompt_toolchain_swap).
_TOOLCHAIN_UNITS = (
    ("gcc", "gcc", "g++"),
    ("clang", "clang", "clang++"),
)


def _available_toolchain_units():
    """The toolchain units whose cc *and* cxx are both resolvable on PATH.

    Enumerating only installed toolchains keeps the menu honest — offering a
    ``clang`` unit on a machine without clang would just swap one build failure
    for another."""
    return [
        (name, cc, cxx)
        for (name, cc, cxx) in _TOOLCHAIN_UNITS
        if shutil.which(cc) and shutil.which(cxx)
    ]


def _prompt_toolchain_swap(resolved_profile):
    """Prompt for a coherent compiler/linker swap.

    Returns ``(cc, cxx, ld)`` for a retry, or ``None`` if the user backs out
    (re-show the top recovery menu). CC/CXX always come from one toolchain unit
    (2.1.0-B1); ``[m]`` is an advanced escape hatch for a hand-entered pair, and
    LD is prompted separately (linker choice is orthogonal to the compiler)."""
    cur_cc = resolved_profile.get("CC")
    cur_cxx = resolved_profile.get("CXX")
    units = _available_toolchain_units()

    lines = ["Choose a compiler toolchain:"]
    keys: list[str] = []
    for name, cc, cxx in units:
        keys.append(name)
        marker = "  (current)" if cur_cc == cc and cur_cxx == cxx else ""
        lines.append(f"  [{name}] CC={cc}  CXX={cxx}{marker}")
    lines.append("  [m] enter cc/cxx manually")
    lines.append("  [b] back")
    keys.extend(["m", "b"])
    choice = prompt_choice(
        "\n".join(lines) + "\nToolchain: ", tuple(keys),
        default="b", eof_default="b", tag="MAKEPKG",
    )

    if choice == "b":
        return None
    if choice == "m":
        new_cc = prompt_text(f"CC [{cur_cc or 'default'}]: ",
                             default=cur_cc or "", tag="MAKEPKG")
        new_cxx = prompt_text(f"CXX [{cur_cxx or 'default'}]: ",
                              default=cur_cxx or "", tag="MAKEPKG")
    else:
        _name, new_cc, new_cxx = next(u for u in units if u[0] == choice)
    new_ld = prompt_text("LD (e.g. lld, bfd, mold; blank to keep): ",
                         default="", tag="MAKEPKG")
    return new_cc, new_cxx, new_ld


def _run_recovery_menu(pkgbuild_path, conf_path, resolved_profile, *,
                       extra_env, extra_flags, interactive, strip_flags,
                       reemit_conf, pkgbase):
    """Interactive recovery loop. Returns a RecoveryOutcome; only returns on a
    successful retry or an abort. Re-invokes invoke_makepkg internally for the
    editor and swap paths so a still-failing retry re-shows the menu."""
    pkgbuild_path = Path(pkgbuild_path).resolve()
    cc = resolved_profile.get("CC", "(default)")
    cxx = resolved_profile.get("CXX", "(default)")
    ld = _summary_linker(resolved_profile, conf_path)
    have_swap = reemit_conf is not None
    orig_snapshot = pkgbuild_path.with_suffix(pkgbuild_path.suffix + ".orig")

    label = pkgbase or pkgbuild_path.name
    while True:
        _makepkg_log.ui(f"Build failed: {label}")
        _makepkg_log.ui(f"  Toolchain used:  CC={cc}  CXX={cxx}  LD={ld}")
        msg, choices = _recover_menu_choices(have_swap, label)
        choice = prompt_choice(msg, choices, default="r", eof_default="a",
                               tag="MAKEPKG")

        if choice == "a":
            return RecoveryOutcome(action="abort")

        if choice == "e":
            editor, source = resolve_editor()
            if not editor_usable(editor):
                _makepkg_log.error(
                    "No usable $EDITOR (set SYSFORGE_EDITOR or [ui].editor).")
                continue
            if not orig_snapshot.exists():
                try:
                    orig_snapshot.write_text(pkgbuild_path.read_text())
                except OSError as e:
                    _makepkg_log.warn(f"Could not snapshot PKGBUILD.orig: {e}")
            run_tty_argv([editor, str(pkgbuild_path)])
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                               extra_env, extra_flags, interactive, strip_flags)
                return RecoveryOutcome(action="retry")
            except subprocess.CalledProcessError:
                continue  # still failing → re-show menu

        if choice == "c" and have_swap:
            # Present the compiler choice as a coherent toolchain unit (gcc or
            # clang), never two independent free-text prompts — a mixed cc/cxx
            # pair just fails the retry confusingly (2.1.0-B1). A "back" answer
            # (None) re-shows the top recovery menu.
            swap = _prompt_toolchain_swap(resolved_profile)
            if swap is None:
                continue
            new_cc, new_cxx, new_ld = swap
            # CC/CXX are env-delivered (resolve_env_vars injects them, and
            # invoke_makepkg applies extra_env LAST, over the conf). Re-emitting
            # the conf alone is not enough — the stale extra_env["CC"]/["CXX"]
            # would clobber it — so overlay the swap onto a copy of extra_env.
            # LD stays conf-delivered (LDFLAGS) via reemit_conf; not an env key.
            swap_env = dict(extra_env or {})
            if new_cc:
                swap_env["CC"] = new_cc
            if new_cxx:
                swap_env["CXX"] = new_cxx
            try:
                with reemit_conf(new_cc, new_cxx, new_ld) as new_conf:
                    invoke_makepkg(pkgbuild_path, new_conf, resolved_profile,
                                   swap_env, extra_flags, interactive,
                                   strip_flags)
                return RecoveryOutcome(
                    action="retry",
                    overrides={"cc": new_cc, "cxx": new_cxx, "ld": new_ld},
                )
            except subprocess.CalledProcessError:
                _makepkg_log.error(
                    f"Build still failed after compiler swap: {label}")
                continue

        # choice == "r": retry as-is.
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                           extra_env, extra_flags, interactive, strip_flags)
            return RecoveryOutcome(action="retry")
        except subprocess.CalledProcessError:
            continue


def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile,
                       extra_env=None, extra_flags=None, interactive=False,
                       strip_flags=None, *, reemit_conf=None, pkgbase=None):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.

    If makepkg fails but .pkg.tar.* files are present in the build dir, the
    build itself likely succeeded and only the install step failed (e.g. due
    to a sudo timeout). In that case the user is offered a sudo re-auth +
    direct pacman -U path instead of a full rebuild.
    """
    label = pkgbase or Path(pkgbuild_path).name
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                           extra_env, extra_flags, interactive, strip_flags)
        except ToolchainMismatchError:
            # Propagate so _run_build can auto-retry with the GCC flag guard
            # before falling back to normal batch-mode failure handling.
            raise
        except subprocess.CalledProcessError as e:
            _makepkg_log.error(
                f"Build failed in batch mode, aborting ({label}): {e}")
            raise _build_failed_error(e) from e
    else:
        while True:
            try:
                invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                               extra_env, extra_flags, interactive, strip_flags)
                break
            except ToolchainMismatchError:
                # Propagate so _run_build can auto-retry with the GCC flag
                # guard instead of prompting the user for manual correction.
                raise
            except subprocess.CalledProcessError as e:
                _makepkg_log.error(f"Build failed ({label}): {e}")
                _makepkg_log.info(f"PKGBUILD location: {pkgbuild_path}")

                installing = extra_flags and any(f in INSTALL_FLAGS for f in extra_flags)
                built_pkgs = (
                    _find_built_packages(Path(pkgbuild_path).resolve().parent)
                    if installing else []
                )
                if built_pkgs:
                    _makepkg_log.ui(
                            "Built packages found — build likely succeeded but "
                            "install failed (sudo timeout?):")
                    for p in built_pkgs:
                        _makepkg_log.ui(f"  {p.name}")
                    # Empty input (Enter) → "" → falls through to retry the
                    # full build below; "s" → install built packages with
                    # fresh sudo; "abort" → stop.
                    response = prompt_choice(
                        "[s]udo re-auth and install, fix PKGBUILD and press "
                        "Enter to retry, or type 'abort' to stop: ",
                        choices=("s", "abort"),
                        default="",
                        eof_default="abort",
                        tag="MAKEPKG",
                    )
                    if response == "s":
                        while True:
                            _makepkg_log.ui("Refreshing sudo credentials...")
                            subprocess.run(["sudo", "-v"])
                            result = subprocess.run(
                                ["sudo", "pacman", "-U", "--noconfirm"]
                                + [str(p) for p in built_pkgs]
                            )
                            if result.returncode == 0:
                                _makepkg_log.ui("Install succeeded.")
                                return
                            _makepkg_log.error(
                                       f"pacman -U failed (exit {result.returncode})")
                            retry = prompt_choice(
                                "Retry install? [s]udo re-auth again, or 'abort': ",
                                choices=("s", "abort"),
                                default="abort",
                                eof_default="abort",
                                tag="MAKEPKG",
                            )
                            if retry != "s":
                                raise _build_failed_error(
                                    e,
                                    "[build_failed] Aborted by user after install failure",
                                ) from e
                    elif response == "abort":
                        raise _build_failed_error(
                            e, "[build_failed] Aborted by user after build failure"
                        ) from e
                    # anything else: fall through to retry the full build
                    _makepkg_log.info("Retrying build...")
                else:
                    outcome = _run_recovery_menu(
                        pkgbuild_path, conf_path, resolved_profile,
                        extra_env=extra_env, extra_flags=extra_flags,
                        interactive=interactive, strip_flags=strip_flags,
                        reemit_conf=reemit_conf, pkgbase=pkgbase)
                    if outcome.action == "abort":
                        raise _build_failed_error(
                            e, "[build_failed] Aborted by user after build failure"
                        ) from e
                    # Menu's retry already ran a successful build. This function
                    # returns None, so surface any recovered overrides to the
                    # caller through the read-once _LAST_RECOVERY contextvar
                    # (drained by take_last_recovery) instead of changing the
                    # return contract.
                    _LAST_RECOVERY.set(outcome)
                    return
