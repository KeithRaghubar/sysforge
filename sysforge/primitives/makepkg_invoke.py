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
import os
import subprocess
import sys
from pathlib import Path

from sysforge import log
from sysforge.primitives.makepkg_artifacts import _find_built_packages
from sysforge.primitives.makepkg_env import _effective_build_dir
from sysforge.primitives.makepkg_flags import INSTALL_FLAGS
from sysforge.primitives.profile import CONF_KEY_MAP
from sysforge.primitives.prompt import prompt_choice
from sysforge.primitives.pty_runner import run_with_pty, strip_ansi
from sysforge.primitives.resource_guard import lift_for_child

_makepkg_log = log.get_logger("MAKEPKG")


# Heartbeat cadence for invoke_makepkg's pty loop. Ninja attached to a pty
# uses \r to redraw a single status line in place and rarely emits \n during
# a long compile phase, so without a heartbeat both the terminal (under -vvv)
# and the per-package log stay silent for many minutes even though the build
# is healthy. The pty runner uses this to surface the latest \r-overwritten
# segment (or a "no output" marker) at this cadence.
MAKEPKG_HEARTBEAT_S = 30.0

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
            _makepkg_log.info(f"Stripped from shell env (superseded by profile): {k}={env.pop(k)!r}")

    # LLVM_PROFILE_FILE is only meaningful during PGO pass 2, where the
    # toolchain stage injects it via extra_env.  If inherited from the shell
    # (e.g. user .zprofile), it causes every clang invocation to emit profraw
    # files into an unrelated directory.
    _llvm_pf = env.pop("LLVM_PROFILE_FILE", None)
    if _llvm_pf:
        _makepkg_log.info(f"Stripped inherited LLVM_PROFILE_FILE={_llvm_pf!r} (only set during PGO pass 2)")

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
    cmd = ["makepkg", "-p", pkgbuild_path.name] + flags

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
            preexec_fn=lift_for_child,
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
                diag_dir = _effective_build_dir(pkgbuild_path, resolved_profile, env)
                _suggestions = _build_diagnose([], diag_dir)
                if _suggestions:
                    _makepkg_log.info(_render_diag_suggestions(_suggestions))
                    cpe.diagnosis = _suggestions
            except Exception as _diag_e:
                _makepkg_log.debug(f"interactive postflight diagnosis skipped: {_diag_e}")
            cpe.captured_output = []
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
    returncode = run_with_pty(
        cmd, cwd=build_dir, env=env,
        line_callback=_on_line,
        forward_bytes=forward_bytes,
        preexec_fn=lift_for_child,
        idle_callback=_on_idle,
        idle_timeout_s=MAKEPKG_HEARTBEAT_S,
    )

    if returncode != 0:
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
            _makepkg_log.info("re-run with -vvv to capture full makepkg output "
                      "in the log for diagnosis")
        if toolchain_mismatch:
            _makepkg_log.warn(
                "Detected clang-only compiler flag rejected by GCC — "
                "the package's build system likely invokes a hardcoded gcc/g++"
            )
            err = ToolchainMismatchError(returncode, "makepkg")
            err.captured_output = captured_lines
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
        cpe.captured_output = captured_lines  # consumed by auto_repair
        # Postflight diagnosis (build_diag suggestions) — carried up so the
        # caller (sysforge update) can persist signature/fix_cmd alongside the
        # failure in build_state.toml. List of FixSuggestion; [] when nothing
        # matched.
        cpe.diagnosis = _suggestions
        raise cpe

def _build_failed_error(cause: Exception, message: str | None = None) -> RuntimeError:
    """Wrap a build failure in the ``[build_failed]`` RuntimeError, preserving
    the postflight ``diagnosis`` and ``captured_output`` from the underlying
    CalledProcessError so `sysforge update` can persist them to build_state.

    ``message`` overrides the default ``[build_failed] <cause>`` text (used by
    the interactive user-abort raises, which keep their own wording but still
    carry the diagnosis recovered from the build's side-car logs)."""
    err = RuntimeError(message or f"[build_failed] {cause}")
    err.diagnosis = getattr(cause, "diagnosis", None)
    err.captured_output = getattr(cause, "captured_output", None)
    return err

def _invoke_with_retry(pkgbuild_path, conf_path, resolved_profile,
                       extra_env=None, extra_flags=None, interactive=False,
                       strip_flags=None):
    """
    Invoke makepkg, retrying after manual correction if not in batch mode.

    If makepkg fails but .pkg.tar.* files are present in the build dir, the
    build itself likely succeeded and only the install step failed (e.g. due
    to a sudo timeout). In that case the user is offered a sudo re-auth +
    direct pacman -U path instead of a full rebuild.
    """
    if resolved_profile.get("batch", False):
        try:
            invoke_makepkg(pkgbuild_path, conf_path, resolved_profile,
                           extra_env, extra_flags, interactive, strip_flags)
        except ToolchainMismatchError:
            # Propagate so _run_build can auto-retry with the GCC flag guard
            # before falling back to normal batch-mode failure handling.
            raise
        except subprocess.CalledProcessError as e:
            _makepkg_log.error(f"Build failed in batch mode, aborting: {e}")
            raise _build_failed_error(e)
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
                _makepkg_log.error(f"Build failed: {e}")
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
                    from sysforge.ui import progress as _ui_progress
                    _ui_progress.clear()
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
                            from sysforge.ui import progress as _ui_progress
                            _ui_progress.clear()
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
                                )
                    elif response == "abort":
                        raise _build_failed_error(
                            e, "[build_failed] Aborted by user after build failure"
                        )
                    # anything else: fall through to retry the full build
                    _makepkg_log.info("Retrying build...")
                else:
                    from sysforge.ui import progress as _ui_progress
                    _ui_progress.clear()
                    # Empty input → "" → retry; "abort" → stop.
                    response = prompt_choice(
                        "Manually correct the PKGBUILD and press Enter to retry, "
                        "or type 'abort' to stop: ",
                        choices=("abort",),
                        default="",
                        eof_default="abort",
                        tag="MAKEPKG",
                    )
                    if response == "abort":
                        raise _build_failed_error(
                            e, "[build_failed] Aborted by user after build failure"
                        )
                    _makepkg_log.info("Retrying build...")
