# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
env_chain.py — snapshot, relay, and diff the runtime env chain sysforge sees.

The sysforge process inherits its environment from a chain that on a typical
Arch + COSMIC workstation looks like:

    systemd-pid1
      └── greetd                     (display-manager.service)
            └── cosmic-greeter
                  └── cosmic-session   (reads /etc/environment, PAM env,
                                        systemd-user env, /etc/profile-style
                                        files depending on greeter config)
                        └── cosmic-comp (Wayland compositor — inherits)
                              └── terminal-emulator
                                    └── zsh (login? interactive?)
                                          └── python → sysforge

Each link can mutate the environment. When sysforge sees a missing or
unexpected var, pinpointing the broken link is the actual debug task.

This module collects every source that contributes to the inherited env
plus the runtime view, and surfaces *divergences* — vars where two
sources disagree, or where a source declares a value the runtime doesn't
carry.

Sources read:
    runtime           os.environ at process startup
    etc_environment   /etc/environment (bare KEY=value, no `export`)
    pam_env_default   /etc/security/pam_env.conf DEFAULT field
    pam_env_override  /etc/security/pam_env.conf OVERRIDE field
    system_zshenv     /etc/zsh/zshenv
    user_zshenv       ~/.zshenv
    system_zprofile   /etc/zsh/zprofile
    user_zprofile     ~/.zprofile
    etc_profile       /etc/profile
    user_profile      ~/.profile
    system_zshrc      /etc/zsh/zshrc
    user_zshrc        ~/.zshrc
    system_zlogin     /etc/zsh/zlogin
    user_zlogin       ~/.zlogin
    systemd_user      `systemctl --user show-environment` (skipped if no XDG_RUNTIME_DIR)
    sysforge_config   resolved [defaults] profile from profiles.toml

Init-file parsing is regex-based, not subshell-based: sourcing in a
clean subshell would execute arbitrary code on every sysforge command
(direnv hooks, ssh-agent loaders, …). Patterns matched per line:

    export KEY=value
    KEY=value; export KEY
    KEY=value         (only accepted in /etc/environment and pam_env)

A single matching pair of surrounding ``"`` or ``'`` is stripped. Values
containing ``$(`` / backtick / ``${VAR}`` are kept raw and tagged
``<expansion: …>``; ``parse_caveats`` carries the skipped-line count
per source.

Cost: ~35–75ms per invocation depending on whether the systemd-user
subprocess fires (the dominant cost). Subprocess is skipped entirely
when ``XDG_RUNTIME_DIR`` is unset, dropping the total to ~10ms. The
cost lands on *every* sysforge command, not only ``-vvv`` — verbosity
gates console output, not collection.

Public API:
    collect_env_chain()       → EnvChainSnapshot
    compute_divergences(snap) → dict[str, dict[str, str]]
    format_env_chain(snap, *, verbosity=0) → str
    validate_env_chain(snap)  → list[str]
    log_env_chain(level="debug") → EnvChainSnapshot
"""
from __future__ import annotations

import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Var groups (display only — divergence works against the flat union)
# ---------------------------------------------------------------------------

_SYSFORGE_VARS = (
    "SYSFORGE_STATE_DIR",
    "SYSFORGE_CONFIG_DIR",
)
_TOOLCHAIN_VARS = (
    "CC", "CXX", "AR", "NM", "RANLIB", "LD",
    "CFLAGS", "CXXFLAGS", "LDFLAGS", "MAKEFLAGS",
    "RUSTC_WRAPPER",
    "LLVM_PROFILE_FILE",
)
_MAKEPKG_VARS = (
    "BUILDDIR", "PKGDEST", "SRCDEST", "SRCPKGDEST", "LOGDEST",
    "CHROOT", "MAKEPKG_CONF",
)
_PYTHON_VARS = (
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME",
)
_DESKTOP_VARS = (
    "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP", "DESKTOP_SESSION",
    "WAYLAND_DISPLAY", "DISPLAY",
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
)
_SHELL_VARS = (
    "SHELL", "TERM", "USER", "HOME", "LOGNAME", "PWD",
)


# Init files in load order. (source_name, path, allow_bare_assignments)
def _user_init_files() -> list[tuple[str, str, bool]]:
    home = os.environ.get("HOME")
    sys_files: list[tuple[str, str, bool]] = [
        ("system_zshenv",   "/etc/zsh/zshenv",    False),
        ("system_zprofile", "/etc/zsh/zprofile",  False),
        ("etc_profile",     "/etc/profile",       False),
        ("system_zshrc",    "/etc/zsh/zshrc",     False),
        ("system_zlogin",   "/etc/zsh/zlogin",    False),
    ]
    user_files: list[tuple[str, str, bool]] = []
    if home:
        user_files = [
            ("user_zshenv",   str(Path(home) / ".zshenv"),   False),
            ("user_zprofile", str(Path(home) / ".zprofile"), False),
            ("user_profile",  str(Path(home) / ".profile"),  False),
            ("user_zshrc",    str(Path(home) / ".zshrc"),    False),
            ("user_zlogin",   str(Path(home) / ".zlogin"),   False),
        ]
    return sys_files + user_files


_SHELL_INIT_FILES = (
    "/etc/zsh/zshenv",
    "/etc/zsh/zprofile",
    "/etc/zsh/zshrc",
    "/etc/zsh/zlogin",
    "/etc/profile",
    "/etc/environment",
)
_USER_SHELL_INIT_FILES = (
    ".zshenv",
    ".zprofile",
    ".zshrc",
    ".zlogin",
    ".profile",
    ".bash_profile",
    ".bashrc",
)


# Init-file parse patterns (compiled once at module load).
_RE_EXPORT_KV = re.compile(r"^\s*export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_RE_KV_EXPORT = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*?)\s*;\s*export\s+\1\s*$"
)
_RE_BARE_KV = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_RE_EXPANSION = re.compile(r"\$\(|`|\$\{")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class ProcessLink:
    """One node in the parent-process chain."""
    pid: int
    comm: str
    cmdline: str = ""


@dataclass
class EnvChainSnapshot:
    """A point-in-time view of the env chain sysforge has inherited.

    ``sources`` is the authoritative diff-substrate: it includes a flat
    ``runtime`` view plus one entry per parseable source. The grouped
    fields (``sysforge``/``toolchain``/…) exist for the human-readable
    layout only.
    """
    pid: int = 0
    sysforge: dict[str, str | None] = field(default_factory=dict)
    toolchain: dict[str, str | None] = field(default_factory=dict)
    makepkg: dict[str, str | None] = field(default_factory=dict)
    python: dict[str, str | None] = field(default_factory=dict)
    desktop: dict[str, str | None] = field(default_factory=dict)
    shell: dict[str, str | None] = field(default_factory=dict)
    path: str = ""
    process_chain: list[ProcessLink] = field(default_factory=list)
    shell_init_files: dict[str, bool] = field(default_factory=dict)
    sources: dict[str, dict[str, str]] = field(default_factory=dict)
    parse_caveats: dict[str, int] = field(default_factory=dict)
    sysforge_config_profile: str | None = None
    config_load_error: str | None = None
    cost_ms: int = 0


# ---------------------------------------------------------------------------
# Init-file / env-file parsing
# ---------------------------------------------------------------------------

def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _parse_value(raw: str) -> tuple[str, bool]:
    """Return ``(cleaned_value, had_expansion)``. ``had_expansion`` is True
    when the value contains shell expansion we don't try to evaluate; the
    raw text is preserved verbatim with an ``<expansion: …>`` marker."""
    inline_comment_idx = -1
    in_single = False
    in_double = False
    for i, ch in enumerate(raw):
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "#" and not in_single and not in_double:
            inline_comment_idx = i
            break
    body = raw if inline_comment_idx < 0 else raw[:inline_comment_idx]
    body = body.rstrip()
    cleaned = _strip_quotes(body)
    if _RE_EXPANSION.search(cleaned):
        return f"<expansion: {cleaned}>", True
    return cleaned, False


def _parse_shell_init_file(
    path: Path, *, allow_bare: bool = False,
) -> tuple[dict[str, str], int]:
    """Regex-extract ``export KEY=value`` patterns. Returns
    ``(kv, caveats)`` where caveats counts lines that look assignment-like
    but were skipped (e.g. arithmetic, function bodies)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, 0
    kv: dict[str, str] = {}
    caveats = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = _RE_EXPORT_KV.match(line) or _RE_KV_EXPORT.match(line)
        if m:
            key, raw = m.group(1), m.group(2)
            value, _ = _parse_value(raw)
            kv[key] = value
            continue
        if allow_bare:
            m = _RE_BARE_KV.match(line)
            if m:
                key, raw = m.group(1), m.group(2)
                value, _ = _parse_value(raw)
                kv[key] = value
                continue
        if "=" in stripped and not stripped.startswith(("if ", "for ", "while ", "case ")):
            caveats += 1
    return kv, caveats


def _read_etc_environment() -> tuple[dict[str, str], int]:
    return _parse_shell_init_file(Path("/etc/environment"), allow_bare=True)


def _read_pam_env() -> tuple[dict[str, str], dict[str, str], int]:
    """Parse /etc/security/pam_env.conf into (defaults, overrides, caveats).

    Format per pam_env(5):  VARIABLE  [DEFAULT="value"]  [OVERRIDE="value"]
    """
    path = Path("/etc/security/pam_env.conf")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}, {}, 0
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    caveats = 0
    field_re = re.compile(r'(DEFAULT|OVERRIDE)=("[^"]*"|\'[^\']*\'|\S+)')
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, *_ = line.split(None, 1)
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", head):
            caveats += 1
            continue
        for m in field_re.finditer(line):
            kind, val = m.group(1), _strip_quotes(m.group(2))
            if kind == "DEFAULT":
                defaults[head] = val
            else:
                overrides[head] = val
    return defaults, overrides, caveats


def _read_systemd_user_env() -> dict[str, str]:
    """Run ``systemctl --user show-environment`` and parse the KEY=value
    output. Returns an empty dict on any failure. Skipped entirely when
    ``XDG_RUNTIME_DIR`` is unset — without a user session the subprocess
    will fail anyway, and skipping saves the 25-60ms fork cost on cron
    or CI invocations."""
    if not os.environ.get("XDG_RUNTIME_DIR"):
        return {}
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}
    out: dict[str, str] = {}
    for line in result.stdout.splitlines():
        m = _RE_BARE_KV.match(line)
        if m:
            out[m.group(1)] = _strip_quotes(m.group(2))
    return out


def _read_sysforge_config() -> tuple[str | None, dict[str, str], str | None]:
    """Resolve the ``[defaults].profile`` from profiles.toml into a flat
    KEY=value dict via the existing profile.merge_extends + serialize_flags
    helpers. Returns ``(profile_name, kv, error)`` — ``error`` is None
    on success."""
    try:
        from sysforge.primitives.config import load_config
        from sysforge.primitives.profile import merge_extends, serialize_flags
        config = load_config()
    except FileNotFoundError as e:
        return None, {}, f"no profiles.toml found: {e}"
    except (OSError, tomllib.TOMLDecodeError, ValueError) as e:
        return None, {}, f"failed to load profiles.toml: {e}"
    defaults = config.get("defaults", {})
    profile_name = defaults.get("profile", "bare")
    profiles = config.get("profiles", {})
    try:
        resolved = merge_extends(profile_name, profiles)
    except ValueError as e:
        return profile_name, {}, f"failed to resolve profile {profile_name!r}: {e}"
    flat = serialize_flags(resolved)
    kv: dict[str, str] = {}
    for line in flat.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            kv[k] = v
    return profile_name, kv, None


# ---------------------------------------------------------------------------
# Existing helpers (kept verbatim from the prior revision)
# ---------------------------------------------------------------------------

def _read_group(names) -> dict[str, str | None]:
    return {name: os.environ.get(name) for name in names}


def _read_process_chain(start_pid: int | None = None, *, max_depth: int = 12) -> list[ProcessLink]:
    pid = start_pid if start_pid is not None else os.getpid()
    chain: list[ProcessLink] = []
    for _ in range(max_depth):
        try:
            status = Path(f"/proc/{pid}/status").read_text(encoding="utf-8")
        except OSError:
            break
        ppid = 0
        for line in status.splitlines():
            if line.startswith("PPid:"):
                ppid = int(line.split()[1])
                break
        try:
            comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
        except OSError:
            comm = "?"
        try:
            raw = Path(f"/proc/{pid}/cmdline").read_bytes()
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
        except OSError:
            cmdline = ""
        chain.append(ProcessLink(pid=pid, comm=comm, cmdline=cmdline))
        if ppid in (0, 1) or ppid == pid:
            if ppid == 1:
                try:
                    comm1 = Path("/proc/1/comm").read_text(encoding="utf-8").strip()
                except OSError:
                    comm1 = "init"
                chain.append(ProcessLink(pid=1, comm=comm1, cmdline=""))
            break
        pid = ppid
    return chain


def _read_shell_init_files_presence() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for p in _SHELL_INIT_FILES:
        out[p] = Path(p).is_file()
    home = os.environ.get("HOME")
    if home:
        for name in _USER_SHELL_INIT_FILES:
            p = str(Path(home) / name)
            out[p] = Path(p).is_file()
    return out


def _collect_sources(snap: EnvChainSnapshot) -> None:
    """Populate ``snap.sources`` and ``snap.parse_caveats``. Mutates snap."""
    # runtime — flat union of every tracked var that has a value, plus a
    # null-marker entry for tracked vars that are unset (so divergence
    # against a source-declared value is detectable).
    runtime: dict[str, str] = {}
    tracked = (
        _SYSFORGE_VARS + _TOOLCHAIN_VARS + _MAKEPKG_VARS
        + _PYTHON_VARS + _DESKTOP_VARS + _SHELL_VARS
    )
    for k in tracked:
        v = os.environ.get(k)
        if v is not None:
            runtime[k] = v
    snap.sources["runtime"] = runtime

    # /etc/environment
    kv, caveats = _read_etc_environment()
    if kv:
        snap.sources["etc_environment"] = kv
    if caveats:
        snap.parse_caveats["etc_environment"] = caveats

    # PAM env
    defaults, overrides, caveats = _read_pam_env()
    if defaults:
        snap.sources["pam_env_default"] = defaults
    if overrides:
        snap.sources["pam_env_override"] = overrides
    if caveats:
        snap.parse_caveats["pam_env"] = caveats

    # Shell init files in load order.
    for source_name, path, allow_bare in _user_init_files():
        kv, caveats = _parse_shell_init_file(Path(path), allow_bare=allow_bare)
        if kv:
            snap.sources[source_name] = kv
        if caveats:
            snap.parse_caveats[source_name] = caveats

    # systemd user env (XDG-gated).
    sysd = _read_systemd_user_env()
    if sysd:
        snap.sources["systemd_user"] = sysd

    # sysforge config defaults.
    profile_name, cfg_kv, err = _read_sysforge_config()
    snap.sysforge_config_profile = profile_name
    snap.config_load_error = err
    if cfg_kv:
        snap.sources["sysforge_config"] = cfg_kv


def collect_env_chain() -> EnvChainSnapshot:
    """Snapshot the env, parent chain, shell init layout, and all
    contributing sources. Populates ``cost_ms`` with the collection time."""
    start = time.monotonic_ns()
    snap = EnvChainSnapshot(
        pid=os.getpid(),
        sysforge=_read_group(_SYSFORGE_VARS),
        toolchain=_read_group(_TOOLCHAIN_VARS),
        makepkg=_read_group(_MAKEPKG_VARS),
        python=_read_group(_PYTHON_VARS),
        desktop=_read_group(_DESKTOP_VARS),
        shell=_read_group(_SHELL_VARS),
        path=os.environ.get("PATH", ""),
        process_chain=_read_process_chain(),
        shell_init_files=_read_shell_init_files_presence(),
    )
    _collect_sources(snap)
    snap.cost_ms = (time.monotonic_ns() - start) // 1_000_000
    return snap


# ---------------------------------------------------------------------------
# Divergence
# ---------------------------------------------------------------------------

def compute_divergences(snap: EnvChainSnapshot) -> dict[str, dict[str, str]]:
    """For each var, build ``{source_name: value}`` only including sources
    that mention the var. A divergence is any var with more than one
    distinct value across its sources, OR a var declared by some source
    where ``runtime`` doesn't have it.

    Returns a dict keyed by variable name. Vars where every source agrees
    are omitted. Sorted alphabetically by key when iterated.
    """
    # Collect every key across all sources.
    all_keys: set[str] = set()
    for src in snap.sources.values():
        all_keys.update(src.keys())
    out: dict[str, dict[str, str]] = {}
    for key in sorted(all_keys):
        per_source: dict[str, str] = {}
        for source_name, kv in snap.sources.items():
            if key in kv:
                per_source[source_name] = kv[key]
        if len(set(per_source.values())) > 1:
            out[key] = per_source
            continue
        # All defining sources agree, but if any non-runtime source
        # declares it and runtime doesn't, that's still a divergence.
        if "runtime" not in per_source and len(per_source) >= 1:
            out[key] = per_source
    return out


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_env_chain(snap: EnvChainSnapshot) -> list[str]:
    warnings: list[str] = []
    if snap.sysforge.get("SYSFORGE_STATE_DIR") is None:
        warnings.append(
            "SYSFORGE_STATE_DIR is unset — sysforge will use the XDG fallback "
            "($XDG_STATE_HOME/sysforge, default ~/.local/state/sysforge). If "
            "this is unexpected, your shell init "
            "files may not be exporting it for this kind of shell (login vs "
            "interactive vs non-interactive)."
        )
    if snap.python.get("VIRTUAL_ENV") is None:
        warnings.append(
            "VIRTUAL_ENV is unset — running outside a Python venv. This is fine "
            "for an installed sysforge but unusual for a dev checkout."
        )
    if snap.toolchain.get("CC") is None and snap.toolchain.get("CXX") is None:
        warnings.append(
            "Neither CC nor CXX is exported. makepkg will use whatever its "
            "configured toolchain decides; sysforge profile overrides still "
            "apply at build time."
        )
    return warnings


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _annotate_var(
    key: str,
    runtime_value: str | None,
    divergences: dict[str, dict[str, str]],
) -> str:
    """Return a bracketed `[differs from …]` annotation for a single var,
    or empty string when there's no divergence to show."""
    if key not in divergences:
        return ""
    per_source = divergences[key]
    others: list[str] = []
    for src_name in sorted(per_source):
        if src_name == "runtime":
            continue
        val = per_source[src_name]
        if runtime_value is not None and val == runtime_value:
            continue
        # Annotate sysforge_config with its profile name when available.
        others.append(f"{src_name}={val}")
    if not others:
        return ""
    return f"  [differs from {', '.join(others)}]"


def _format_group(
    title: str,
    group: dict[str, str | None],
    divergences: dict[str, dict[str, str]] | None = None,
) -> list[str]:
    lines = [f"  {title}:"]
    for k, v in group.items():
        base = f"    {k} = <unset>" if v is None else f"    {k} = {v}"
        if divergences is not None:
            base += _annotate_var(k, v, divergences)
        lines.append(base)
    return lines


def _format_mismatches(snap: EnvChainSnapshot, divergences: dict[str, dict[str, str]]) -> list[str]:
    if not divergences:
        return []
    profile = snap.sysforge_config_profile or "?"
    lines = ["  mismatches:"]
    for key in sorted(divergences):
        per_source = divergences[key]
        lines.append(f"    {key}:")
        # Pad source name column so values line up.
        width = max(len(s) for s in per_source) if per_source else 0
        # Make sure "runtime" appears first, then alphabetical.
        order = sorted(per_source, key=lambda s: (s != "runtime", s))
        runtime_val = per_source.get("runtime")
        if "runtime" not in per_source:
            lines.append(f"      {'runtime'.ljust(width)} = <unset>")
        for src in order:
            val = per_source[src]
            tail = f"   [defaults.{profile}]" if src == "sysforge_config" else ""
            # Suppress noise: if runtime is shown explicitly elsewhere we
            # still want it as the first line for context.
            if src == "runtime" and runtime_val is None:
                continue
            lines.append(f"      {src.ljust(width)} = {val}{tail}")
    return lines


def format_env_chain(snap: EnvChainSnapshot, *, verbosity: int = 0) -> str:
    """Render an ``EnvChainSnapshot`` as a multi-line human-readable string.

    ``verbosity`` controls inline annotations on the grouped sections:

    * ``< 2`` — bare groups; mismatches block is appended at the bottom.
    * ``>= 2`` — each diverging var carries an inline
      ``[differs from …]`` annotation alongside the bare value, in
      addition to the mismatches block.
    """
    divergences = compute_divergences(snap)
    annotate = divergences if verbosity >= 2 else None

    lines: list[str] = [f"env chain (pid={snap.pid}):"]
    lines.extend(_format_group("sysforge", snap.sysforge, annotate))
    lines.extend(_format_group("toolchain", snap.toolchain, annotate))
    lines.extend(_format_group("makepkg", snap.makepkg, annotate))
    lines.extend(_format_group("python", snap.python, annotate))
    lines.extend(_format_group("desktop", snap.desktop, annotate))
    lines.extend(_format_group("shell", snap.shell, annotate))

    lines.append("  PATH:")
    for entry in snap.path.split(":"):
        lines.append(f"    {entry}")

    lines.append("  process chain (sysforge → init):")
    for link in snap.process_chain:
        label = f"pid={link.pid} {link.comm}"
        if link.cmdline:
            cmd = link.cmdline if len(link.cmdline) <= 120 else link.cmdline[:117] + "..."
            label = f"{label}  ({cmd})"
        lines.append(f"    {label}")

    lines.append("  shell init files (exists?):")
    for path, exists in snap.shell_init_files.items():
        lines.append(f"    [{'x' if exists else ' '}] {path}")

    lines.append("  contributing sources:")
    for source_name in sorted(snap.sources):
        count = len(snap.sources[source_name])
        lines.append(f"    {source_name}: {count} var(s)")
    if snap.sysforge_config_profile and "sysforge_config" in snap.sources:
        lines.append(
            f"    (sysforge_config uses [defaults] profile = "
            f"{snap.sysforge_config_profile!r})"
        )
    if snap.config_load_error:
        lines.append(f"    sysforge_config: not loaded — {snap.config_load_error}")
    if snap.parse_caveats:
        lines.append("  parse caveats (lines skipped — expansion or non-trivial syntax):")
        for name in sorted(snap.parse_caveats):
            lines.append(f"    {name}: {snap.parse_caveats[name]}")

    lines.extend(_format_mismatches(snap, divergences))

    warnings = validate_env_chain(snap)
    if warnings:
        lines.append("  warnings:")
        for w in warnings:
            lines.append(f"    - {w}")

    lines.append(f"  collected in {snap.cost_ms}ms")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Logging integration
# ---------------------------------------------------------------------------

def log_env_chain(level: str = "debug") -> EnvChainSnapshot:
    """Collect, format, and emit the env chain via ``sysforge.log``.

    Returns the snapshot so callers can also inspect it programmatically.
    The console output respects the chosen level (default DEBUG = only at
    ``-vvv``); the file log always receives the full snapshot regardless
    of verbosity. Inline annotations on grouped sections appear when the
    current global verbosity is ≥ 2 (``-vv``).
    """
    from sysforge import log as _log
    snap = collect_env_chain()
    rendered = format_env_chain(snap, verbosity=_log.get_verbosity())
    # [ENV_CHAIN]: the OS env-inheritance diagnostic, distinct from makepkg_env's
    # [ENV] (compiler/build env-var resolution) — same word, different concern.
    logger = _log.get_logger("ENV_CHAIN")
    emit = getattr(logger, level.lower(), logger.debug)
    emit(rendered)
    return snap
