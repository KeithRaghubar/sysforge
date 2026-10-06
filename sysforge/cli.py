# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
cli.py — SysForge command-line interface

Top-level commands:
    sysforge build <pkg>    Build a package using its matched profile
    sysforge update         Check for and rebuild outdated sysforge-managed packages
                            (also reports flag/toolchain drift; --rebuild-on-*-drift to act)
    sysforge resolve <pkg>  Show which profile would be applied to a package
    sysforge doctor [PKG]   Health-check installed package depends + linkage

Namespaces:
    sysforge packages       Manage packages.toml (list / add / remove / sync)
    sysforge run            Execute pipeline stages (pipeline / hardware / reconfigure /
                             toolchain / packages / kernel)

Every verb is a ``Verb`` subclass dispatched through
:func:`sysforge.verbs.runner.run_verb`. The pre_check / execute /
post_validate split is documented in DESIGN.md §CLI Verb Framework.
"""
import os
import sys
from pathlib import Path

from sysforge import log

_log = log.get_logger("CLI")

from sysforge.verbs import registry, run_verb


# ---------------------------------------------------------------------------
# Argv preprocessing
# ---------------------------------------------------------------------------

def _hoist_verbosity_flags(argv):
    """
    Move any -v / -vv / --verbose flags to before the subcommand so argparse
    sees them as global flags regardless of where the user placed them.

    sysforge build PKGBUILD -vv  →  sysforge -vv build PKGBUILD
    sysforge build PKGBUILD -v --interactive  →  sysforge -v build PKGBUILD --interactive
    """
    verbose_tokens = []
    rest = []
    # The global --quiet is hoisted like -v so users can place it after the
    # subcommand (sysforge update --quiet). It is NOT hoisted when the command
    # is `doctor`, which owns a *local* --quiet/-q with different semantics
    # (suppress clean lines) — hoisting would let the global parser eat it. The
    # short -q is doctor-only and never hoisted.
    hoist_quiet = "doctor" not in argv
    for tok in argv:
        if tok in ("-v", "-vv", "-vvv", "--verbose") or tok == "--quiet" and hoist_quiet:
            verbose_tokens.append(tok)
        else:
            rest.append(tok)
    return verbose_tokens + rest


def _resolve_verbosity(args):
    """Resolve the effective stderr verbosity level (0–3), mirroring
    :func:`_resolve_color_mode`.

    Precedence (highest first):
      1. Global ``--quiet`` (``args.quiet_global``) → 0, wins over everything.
      2. Else any ``-v/-vv/-vvv`` passed (``args.verbose`` count > 0) → that level.
      3. Else ``[log] verbosity`` config value, clamped to 0–3.
      4. Else 0.

    An invalid config value (non-int, out of range) or an unreadable config
    degrades gracefully — clamp or ignore, never abort startup — matching the
    posture of ``set_color_mode``/``set_unicode_mode``.
    """
    if getattr(args, "quiet_global", False):
        return 0
    verbose = int(getattr(args, "verbose", 0) or 0)
    if verbose > 0:
        return min(3, verbose)
    try:
        from sysforge.primitives.config import load_sysforge_toml
        raw = (load_sysforge_toml().get("log", {}) or {}).get("verbosity")
    except Exception:
        raw = None
    if isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return max(0, min(3, raw))


# Global flags hoisted before the subcommand by _hoist_global_flags.
# Maps flag → whether it consumes a following value token.
_GLOBAL_HOIST_FLAGS = {
    "--py-profile": False,
    "--py-profile-out": True,
    "--timings": False,
    "--color": True,
    "--no-throttle": False,
    "--turbo": False,
    "--frozen": False,
    "--no-frozen": False,
    "--thaw": True,
}


def _resolve_throttle_override(args):
    """Map the global throttle flags to a build_throttle run-override.

    ``--turbo`` (boost) is the stronger request and wins over ``--no-throttle``
    (bypass); neither present → ``None`` (honour the configured throttle).
    """
    if getattr(args, "turbo", False):
        return "boost"
    if getattr(args, "no_throttle", False):
        return "bypass"
    return None


def _resolve_color_mode(flag_value):
    """Resolve the effective colour mode: ``--color`` flag > ``[ui] color`` config
    > ``"auto"``. Any unexpected config value degrades to ``"auto"`` (log.set_color_mode
    also guards this), and a missing/unreadable config never aborts startup.
    """
    if flag_value:
        return flag_value
    try:
        from sysforge.primitives.config import load_sysforge_toml
        cfg = (load_sysforge_toml().get("ui", {}) or {}).get("color")
    except Exception:
        cfg = None
    return cfg if cfg in ("auto", "always", "never") else "auto"


def _hoist_global_flags(argv):
    """
    Move the global flags in _GLOBAL_HOIST_FLAGS (and their value tokens /
    --flag=value forms) to before the subcommand, mirroring
    _hoist_verbosity_flags so users can place them anywhere.

    sysforge update --py-profile  →  sysforge --py-profile update
    """
    hoisted = []
    rest = []
    toks = iter(argv)
    for tok in toks:
        flag = tok.split("=", 1)[0]
        if flag in _GLOBAL_HOIST_FLAGS:
            hoisted.append(tok)
            if _GLOBAL_HOIST_FLAGS[flag] and "=" not in tok:
                value = next(toks, None)
                if value is not None:
                    hoisted.append(value)
        else:
            rest.append(tok)
    return hoisted + rest


# Flags that sysforge handles or that take a value arg — exclude from implicit
# passthrough.  -v is already stripped by _hoist_verbosity_flags.
_PASSTHROUGH_EXCLUDE = frozenset("hVpmD")

# Subcommands that accept makepkg flag passthrough.
_MAKEPKG_SUBCOMMANDS = frozenset({"build", "update"})


def _extract_implicit_makepkg_flags(argv):
    """
    Detect bare makepkg-style short flags on build/update and rewrite
    them into explicit ``-m`` form so the rest of the pipeline handles them
    uniformly.

    sysforge build ventoy -sfCci  →  sysforge build ventoy -m -sfCci

    A token qualifies when it starts with ``-`` (not ``--``), is longer than
    one character, and no letter after the dash is in _PASSTHROUGH_EXCLUDE
    (letters are not validated against makepkg's flag set — makepkg rejects
    unknown flags itself).  If ``-m`` / ``--makepkg`` is already present the
    implicit flags are still collected and merged.
    """
    sub_idx = None
    for i, tok in enumerate(argv):
        if tok in _MAKEPKG_SUBCOMMANDS:
            sub_idx = i
            break
    if sub_idx is None:
        return argv

    before = list(argv[:sub_idx + 1])
    implicit = []
    rest = []
    for tok in argv[sub_idx + 1:]:
        if (
            tok.startswith("-")
            and not tok.startswith("--")
            and len(tok) > 1
            and all(ch not in _PASSTHROUGH_EXCLUDE for ch in tok[1:])
        ):
            implicit.append(tok)
        else:
            rest.append(tok)

    if not implicit:
        return argv

    merged = "-" + "".join(tok[1:] for tok in implicit)

    for i, tok in enumerate(rest):
        if tok in ("-m", "--makepkg") and i + 1 < len(rest):
            rest[i + 1] = rest[i + 1] + " " + merged
            return before + rest
        if tok.startswith("--makepkg="):
            rest[i] = tok + " " + merged
            return before + rest

    return before + rest + ["-m", merged]


def _patch_makepkg_argv(argv):
    """
    Rewrite -m/-makepkg <value> to --makepkg=<value> when <value> starts with
    '-', so argparse doesn't misinterpret it as a new flag.

    argparse cannot accept option values that start with '-' unless they are
    expressed as --flag=value. This preprocessing step keeps the documented
    UX (sysforge build PKGBUILD -m '-sfci') working as intended.
    """
    result = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in ("-m", "--makepkg") and i + 1 < len(argv):
            val = argv[i + 1]
            if val.startswith("-"):
                result.append(f"--makepkg={val}")
                i += 2
                continue
        result.append(tok)
        i += 1
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

# The parser is assembled from the verb registry (3.2.0-F7). These names stay
# importable from cli for main(), the tests and tools/preflight.sh.
_build_parser = registry.build_parser
tiered_command_order = registry.tiered_command_order


_INSTALL_BEARING_COMMANDS = frozenset(
    {"build", "update", "run", "setup"}
)


def _gate_sentinel_check(args) -> bool:
    """True when ``cli.main`` should call ``check_and_recover_stale_sentinel``.

    Install-bearing commands (``build``/``update``/``run``/``setup``)
    gate on a stale sentinel, except when the invocation is
    explicitly read-only (``--dry-run`` / ``--versions``). The inner verb-runner sentinel
    scope already opts out under ``--dry-run`` (see ``UpdateVerb.pre_check``);
    keeping the outer CLI gate in lockstep avoids blocking ``sysforge update
    --dry-run`` on a sentinel from an earlier mutating run the user is still
    investigating.
    """
    cmd = getattr(args, "command", None)
    if cmd not in _INSTALL_BEARING_COMMANDS:
        return False
    if getattr(args, "versions", False):
        # `update --versions` is a read-only report that exits before the build
        # loop (3.1.0-F6) — same rationale as --dry-run above.
        return False
    return not getattr(args, "dry_run", False)


def _strip_venv_from_path() -> None:
    """Scrub sysforge's own venv from inherited PATH/VIRTUAL_ENV/PYTHONPATH.

    Sysforge is typically launched from `~/src/sysforge/.venv/bin/sysforge`
    after the user's shell has activated the venv. Without this strip, any
    code that captures `os.environ["PATH"]` (e.g. `_stage_env` in the PGO
    toolchain stage) carries `.venv/bin` into the makepkg subprocess, where
    `python -m build`-style PKGBUILD steps resolve `python` to the venv
    interpreter — which lacks PEP-517 deps and dies with `No module named
    build`. Gated on a real venv (`sys.prefix != sys.base_prefix`) so a
    packaged install in `/usr/bin` doesn't accidentally strip `/usr/bin`.
    """
    if sys.prefix == sys.base_prefix:
        return
    venv_bin = str(Path(sys.executable).parent)
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    cleaned = [p for p in path_parts if p != venv_bin]
    if len(cleaned) == len(path_parts):
        return
    os.environ["PATH"] = os.pathsep.join(cleaned)
    os.environ.pop("VIRTUAL_ENV", None)
    os.environ.pop("PYTHONPATH", None)
    _log.info(f"Stripped venv bin from PATH: {venv_bin}")


def main():
    # One home for Ctrl-C (1.2.0-F39): a routine abort must not unwind as a
    # raw traceback. Verbs keep raising KeyboardInterrupt normally — a
    # mutating verb's sentinel_scope persists its recovery sentinel on the
    # way up before this handler sees the interrupt.
    #
    # The progress bar's one lifecycle owner (3.3.0-F2): session() releases
    # the terminal however _main ends, and it exits before these handlers run,
    # so the region is gone before `aborted (Ctrl-C)` prints.
    from sysforge.ui import progress
    try:
        with progress.session():
            _main()
    except KeyboardInterrupt:
        log.error("[SYSFORGE]", "aborted (Ctrl-C)")
        sys.exit(130)  # 128 + SIGINT, the conventional interrupt exit
    except BrokenPipeError:
        # The stdout reader went away (`sysforge state list | head`). Python
        # ignores SIGPIPE, so the write surfaces here instead of killing us —
        # and SIG_DFL is not an option: it would also kill a mutating verb
        # mid-build. Point fd 1 at /dev/null so the interpreter's exit-time
        # flush of the buffered remainder can't fail a second time. (Quitting
        # a *pager* early is absorbed in primitives/pager.py instead.)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        sys.exit(141)  # 128 + SIGPIPE, what the shell reports for `yes | head`


def _main():
    _strip_venv_from_path()
    from sysforge.primitives.resource_guard import install as _install_resource_guard
    _install_resource_guard()
    sys.argv[1:] = _patch_makepkg_argv(
        _extract_implicit_makepkg_flags(
            _hoist_global_flags(_hoist_verbosity_flags(sys.argv[1:]))
        )
    )
    parser = _build_parser()
    args = parser.parse_args()
    log.set_verbosity(_resolve_verbosity(args))
    log.set_color_mode(_resolve_color_mode(getattr(args, "color", None)))
    # Source freeze (3.0.0-F2): resolve once, install before any verb runs, so
    # every egress seam sees the same policy no matter how deep it sits.
    from sysforge.primitives.config import load_sysforge_toml
    from sysforge.primitives.net_policy import resolve_net_policy, set_policy
    _security_cfg = load_sysforge_toml().get("security", {})
    set_policy(resolve_net_policy(args, _security_cfg))
    # Build sandbox (3.1.0-F7): the other half of [security] — freeze_sources
    # gates code *ingress*, this gates blast *radius*. Installed here for the
    # same reason: one resolution, consulted at the invocation seam, so a build
    # path that does not know the gate exists still gets it.
    from sysforge.primitives.build_sandbox import (
        resolve_sandbox,
        set_policy as set_sandbox_policy,
    )
    set_sandbox_policy(resolve_sandbox(_security_cfg))
    # Keyserver key fetches (3.1.0-F8): confirm by default, fail closed off-TTY.
    from sysforge.primitives.build_prep import set_key_fetch_policy
    set_key_fetch_policy(auto=bool(_security_cfg.get("auto_fetch_pgp_keys", False)))
    from sysforge.primitives.build_throttle import set_run_override
    set_run_override(_resolve_throttle_override(args))
    if getattr(args, "dry_run", False):
        log.set_dry_run_mode()
    # Snapshot inherited env at startup — DEBUG-only on stderr (-vvv), always
    # written to the file log. Skipped for the `env` verb to avoid duplicating
    # the output it explicitly prints. Best-effort; never fail startup over it.
    if getattr(args, "command", None) != "env":
        try:
            from sysforge.primitives.env_chain import log_env_chain
            log_env_chain("debug")
        except Exception as _e:  # pragma: no cover - defensive
            log.debug("[ENV]", f"env-chain snapshot failed: {_e}")
    from sysforge.ui import progress
    progress.init()
    # Stale stage-in-progress detection: if a previous install-bearing run
    # (toolchain/kernel/packages) was interrupted before clearing its
    # sentinel, block the next mutating command and offer recovery before
    # proceeding. Read-only commands (env, doctor, resolve, fetch, list,
    # completions) skip the check so users can inspect without recovery.
    # Read-only invocations of install-bearing verbs (e.g. `update --dry-run`)
    # also skip — the inner verb has already opted out of its own sentinel
    # scope, so the entry gate matching that semantics keeps the two in sync.
    if _gate_sentinel_check(args):
        from sysforge.primitives.stage_sentinel import check_and_recover_stale_sentinel
        state_dir = getattr(args, "state_dir", None)
        if not check_and_recover_stale_sentinel(state_dir):
            log.error(
                "[SENTINEL]",
                "Stale stage-in-progress sentinel present; refusing to proceed. "
                "Run the recovery command shown above, or remove the sentinel "
                "file once you have manually verified system consistency.",
            )
            sys.exit(2)
    # First-install init notice (F1): on a fresh package install a marker file
    # is dropped in the state dir; advise running the reconfigure + hardware
    # bootstrap stages until both complete, then self-delete. Best-effort,
    # never blocks. Skip completions (machine-readable output must stay clean).
    if getattr(args, "command", None) != "completions":
        from sysforge.primitives.init_notice import maybe_emit_init_notice
        maybe_emit_init_notice(getattr(args, "state_dir", None))
    verb_cls = getattr(args, "verb_cls", None)
    if verb_cls is None:
        _log.error("No verb dispatcher set for this command — argparse misconfiguration")
        sys.exit(2)
    sys.exit(_dispatch(verb_cls, args))


def _dispatch(verb_cls, args) -> int:
    """Run the verb, optionally under cProfile (--py-profile/--py-profile-out)."""
    if not (args.py_profile or args.py_profile_out):
        return run_verb(verb_cls(), args)
    import cProfile
    import pstats
    prof = cProfile.Profile()
    prof.enable()
    try:
        return run_verb(verb_cls(), args)
    finally:
        # Verbs may sys.exit() inside execute — emit stats regardless.
        prof.disable()
        if args.py_profile_out:
            prof.dump_stats(args.py_profile_out)
        pstats.Stats(prof, stream=sys.stderr).sort_stats("cumulative").print_stats(25)
