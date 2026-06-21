# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
config.py — SysForge config file loading

Responsible for all TOML config file I/O: profiles.toml (which holds flag
profiles, [[rules]], append conflict groups, and consumes inference) and
sysforge.toml global settings. Owns the user/system merge logic.

Public API:
    load_config(config_paths=None)         -> dict
    load_conflict_groups(paths=None)       -> dict
    load_consumes_inference(paths=None)    -> dict
    load_sysforge_toml()                   -> dict
    find_pkgbuild(pkg, config=None)        -> Path   (AUR clone on miss if pkgbuild_src_dir set)
    resolve_pkgbuild_src_dir(config, build_cfg=None) -> str | None
"""
import pprint
import tomllib
from sysforge import log
_log = log.get_logger("CONFIG")
from pathlib import Path

from sysforge.primitives.paths import (
    CONFIG_PATHS,
    SYSFORGE_TOML_PATH,
)

# packages.toml [build] repo_mode values. "build_from_source" replaced the
# legacy "profiled" token (which collided with build_state's build_mode and
# PGO). Legacy files still parse: resolve_repo_mode() maps the old value, and
# the file self-migrates the next time reconfigure rewrites it.
REPO_MODE_PACMAN = "pacman"
REPO_MODE_SOURCE = "build_from_source"
_LEGACY_REPO_MODE_SOURCE = "profiled"

# Per-package opt-in key. "enable_build_from_source" replaced the misleading
# "pkgbuild_patch" (which never patched anything — it forced the source-build
# path for a repo package). Stays boolean. Legacy entries are normalized to the
# new key in expand_package_groups so every manifest consumer sees one name.
PKG_KEY_BUILD_FROM_SOURCE = "enable_build_from_source"
_LEGACY_PKG_KEY_BUILD_FROM_SOURCE = "pkgbuild_patch"


def resolve_repo_mode(build_cfg: dict | None) -> str:
    """Resolve packages.toml ``[build] repo_mode`` to a current-vocabulary value.

    Returns ``"pacman"`` (default) or ``"build_from_source"``. The legacy
    ``"profiled"`` token is mapped to ``"build_from_source"`` so existing
    untracked user configs keep working. This is the single read chokepoint for
    ``repo_mode``; every consumer routes through it instead of reading the raw
    key, so the legacy alias is honored in exactly one place.
    """
    raw = (build_cfg or {}).get("repo_mode", REPO_MODE_PACMAN)
    if raw == _LEGACY_REPO_MODE_SOURCE:
        return REPO_MODE_SOURCE
    return raw


def normalize_package_entry(entry: dict) -> dict:
    """Return ``entry`` with the legacy per-package key renamed in place.

    Renames ``pkgbuild_patch`` → ``enable_build_from_source`` (the new key wins
    if both are present). Mutates and returns the same dict for convenience.
    """
    if _LEGACY_PKG_KEY_BUILD_FROM_SOURCE in entry:
        legacy = entry.pop(_LEGACY_PKG_KEY_BUILD_FROM_SOURCE)
        entry.setdefault(PKG_KEY_BUILD_FROM_SOURCE, legacy)
    return entry


def load_sysforge_toml() -> dict:
    """Load /etc/sysforge/sysforge.toml (global sysforge settings).

    Returns an empty dict if the file is missing or unparseable.
    """
    if not SYSFORGE_TOML_PATH.exists():
        return {}
    try:
        with open(SYSFORGE_TOML_PATH, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        _log.warn(f"Could not load {SYSFORGE_TOML_PATH}: {e}")
        return {}


# One-time guard for the dual-key mismatch warning below — the resolver is
# called per package in some paths and the warning is per-run information.
_src_dir_mismatch_warned = False


def resolve_pkgbuild_src_dir(config: dict | None, build_cfg: dict | None = None) -> str | None:
    """Effective ``pkgbuild_src_dir``: packages.toml ``[build]`` wins over
    profiles.toml ``[paths]``.

    The two keys are allowed to differ (separate configs, separate owners),
    but a silent mismatch means builds and updates can read PKGBUILDs from
    different trees — so when both are set and point at different directories,
    warn once per run naming both values.
    """
    global _src_dir_mismatch_warned
    build_val = (build_cfg or {}).get("pkgbuild_src_dir")
    paths_val = (config or {}).get("paths", {}).get("pkgbuild_src_dir")
    if (
        build_val and paths_val
        and not _src_dir_mismatch_warned
        and Path(build_val).expanduser() != Path(paths_val).expanduser()
    ):
        _src_dir_mismatch_warned = True
        _log.warn(
            "pkgbuild_src_dir mismatch: packages.toml [build] sets "
            f"{build_val!r} but profiles.toml [paths] sets {paths_val!r} — "
            f"using {build_val!r} ([build] takes precedence)"
        )
    return build_val or paths_val


def find_pkgbuild(pkg: str, config: dict | None = None) -> Path:
    """
    Resolve a PKGBUILD path from a pkg argument.

    Search order:
    1. pkg is an existing path → use directly.
    2. <cwd>/<pkg>/PKGBUILD
    3. <config [paths] pkgbuild_src_dir>/<pkg>/PKGBUILD  (if configured)
    4. If not found locally: check pacman sync DBs (pkgctl repo clone) or AUR (aur_clone)
       — only attempted if pkgbuild_src_dir is configured.

    Raises FileNotFoundError listing all searched paths if nothing is found.
    Raises RuntimeError (from pkgctl_checkout/aur_clone) if the clone fails.
    """
    # Inline import to avoid a module-level circular dependency:
    # aur.py → log.py (fine), but keeping aur out of config's top-level
    # imports avoids pulling subprocess/urllib into every config load path.
    from sysforge.primitives.aur import aur_info, is_repo_package, pkgctl_checkout
    from sysforge.primitives.source_sync import SyncRequest, get_scheduler

    clone_timeout = load_sysforge_toml().get("git", {}).get("clone_timeout", 60)

    p = Path(pkg)
    if p.is_dir():
        p = p / "PKGBUILD"
    if p.exists():
        return p.resolve()

    searched: list[Path] = [p]

    cwd_candidate = Path.cwd() / pkg / "PKGBUILD"
    searched.append(cwd_candidate)
    if cwd_candidate.exists():
        return cwd_candidate.resolve()

    if config:
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            pkgbuild_src_dir = Path(raw).expanduser()
            dir_candidate = pkgbuild_src_dir / pkg / "PKGBUILD"
            searched.append(dir_candidate)
            if dir_candidate.exists():
                return dir_candidate.resolve()

            # Not found locally — check repo first, then AUR.
            # Ensure pkgbuild_src_dir exists so subprocess cwd= and git clone
            # don't ENOENT on a fresh system where ~/src hasn't been created yet.
            pkgbuild_src_dir.mkdir(parents=True, exist_ok=True)
            clone_dest = pkgbuild_src_dir / pkg
            if is_repo_package(pkg):
                # raises RuntimeError on failure
                pkgctl_checkout(pkg, clone_dest, timeout=clone_timeout)
                if dir_candidate.exists():
                    return dir_candidate.resolve()
            elif aur_info([pkg]):
                # Route through the scheduler so repeated find_pkgbuild calls
                # for the same pkg (fetch.py → update.py → makepkg_wrapper.py)
                # dedup to a single clone.
                sync_result = get_scheduler().request(SyncRequest(
                    pkgbase=pkg, pkgbuild_dir=clone_dest, source="aur",
                ))
                if sync_result.error and not dir_candidate.exists():
                    raise RuntimeError(
                        f"AUR clone failed for {pkg!r}: {sync_result.error}"
                    )
                if dir_candidate.exists():
                    return dir_candidate.resolve()

    searched_str = "\n".join(f"    {s}" for s in searched)
    raise FileNotFoundError(
        f"PKGBUILD not found for {pkg!r}.\n"
        f"  Searched:\n{searched_str}\n"
        f"  Pass a full path, set [paths] pkgbuild_src_dir in profiles.toml,\n"
        f"  or cd into the package directory and run: sysforge build/resolve <name>"
    )


def expand_package_groups(data: dict) -> list[dict]:
    """Return packages.toml ``[[package]]`` entries with ``[group.*]`` expanded.

    A ``[group.<name>]`` table declares ``packages = ["a", "b", ...]`` plus
    optional per-group defaults (``source`` / ``enable_build_from_source`` /
    ``cache`` / ``reason``) inherited by every member. Expansion appends one
    synthetic entry per member carrying ``group = "<name>"`` to mark its origin;
    an explicit ``[[package]]`` entry for the same name wins outright (no field
    merge), and the first group to claim a name wins over later groups.

    This is also the single normalization point for the legacy per-package key
    ``pkgbuild_patch`` → ``enable_build_from_source`` (via
    ``normalize_package_entry``), so every manifest consumer sees the current
    name regardless of file vintage.

    This is the single expansion point for every manifest consumer (pipeline
    packages stage, update overrides, completions, packages list, reconfigure
    summaries) — do not re-expand ``[group.*]`` anywhere else.
    """
    entries = [normalize_package_entry(dict(e)) for e in data.get("package", [])]
    groups = data.get("group", {}) or {}
    seen = {e.get("name") for e in entries}
    for gname, gtable in groups.items():
        if not isinstance(gtable, dict):
            continue
        defaults = normalize_package_entry(
            {k: v for k, v in gtable.items() if k != "packages"}
        )
        for name in gtable.get("packages", []):
            if not name or name in seen:
                continue
            seen.add(name)
            entries.append({"name": name, **defaults, "group": gname})
    return entries


def load_config(config_paths=None) -> dict:
    """
    Load profiles.toml from CONFIG_PATHS (user, then system).
    If the user config sets extends_system = true, deep-merge onto system config.
    Otherwise the first found file wins outright.
    Rule priorities must be in range 0-99. User rules are bumped by 100 on merge
    to guarantee precedence over system rules (effective range 100-199).
    Raises FileNotFoundError if no config is found.
    Raises ValueError if any rule has an invalid or missing priority.
    """

    def _load(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    def _deep_merge(base, override):
        """Merge override onto base, recursing into dicts."""
        result = dict(base)
        for key, val in override.items():
            if (
                key in result
                and isinstance(result[key], dict)
                and isinstance(val, dict)
            ):
                result[key] = _deep_merge(result[key], val)
            else:
                result[key] = val
        return result

    paths = config_paths if config_paths is not None else CONFIG_PATHS
    user_path = paths[0] if len(paths) > 0 else None
    system_path = paths[1] if len(paths) > 1 else None

    user_config = _load(user_path) if user_path and user_path.exists() else None
    system_config = _load(system_path) if system_path and system_path.exists() else None

    if user_config is None and system_config is None:
        raise FileNotFoundError(
            "[CONFIG] No profiles.toml found. Searched:\n"
            + "\n".join(f"  {p}" for p in CONFIG_PATHS)
        )

    if user_config is None:
        assert system_config is not None  # guaranteed: both-None case raised above
        _validate_rule_priorities(system_config.get("rules", []), "system")
        _log.info(f"Loaded system config: {system_path}")
        _log.debug(f"Full flag_profiles (system):\n{pprint.pformat(system_config, indent=2, sort_dicts=False)}")
        return system_config

    if system_config is None:
        _validate_rule_priorities(user_config.get("rules", []), "user")
        _log.info(f"Loaded user config: {user_path}")
        _log.debug(f"Full flag_profiles (user):\n{pprint.pformat(user_config, indent=2, sort_dicts=False)}")
        return user_config

    if user_config.get("extends_system", False):
        _log.info("Merging user config onto system config")

        merged = _deep_merge(
            {k: v for k, v in system_config.items() if k != "rules"},
            {
                k: v
                for k, v in user_config.items()
                if k not in ("rules", "extends_system")
            },
        )

        _validate_rule_priorities(system_config.get("rules", []), "system")
        _validate_rule_priorities(user_config.get("rules", []), "user")

        user_rules = [
            {**r, "priority": r.get("priority", 0) + 100}
            for r in user_config.get("rules", [])
        ]
        system_rules = system_config.get("rules", [])

        merged["rules"] = system_rules + user_rules
        _log.debug(f"Full flag_profiles (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _validate_rule_priorities(user_config.get("rules", []), "user")
    _log.info(f"User config overrides system config: {user_path}")
    _log.debug(f"Full flag_profiles (user overrides system):\n{pprint.pformat(user_config, indent=2, sort_dicts=False)}")
    return user_config


def _validate_rule_priorities(rules, source):
    VALID_RANGE = range(0, 100)
    for i, rule in enumerate(rules):
        p = rule.get("priority")
        if p is None:
            raise ValueError(
                f"[CONFIG] {source} rule [{i}] is missing required 'priority'"
            )
        if p not in VALID_RANGE:
            raise ValueError(
                f"[CONFIG] {source} rule [{i}] has invalid priority {p!r} "
                f"(must be 0-99)"
            )


def load_conflict_groups(conflict_group_paths=None):
    """
    Load [append_conflict_groups] from profiles.toml (user, then system).
    Returns dict: { group_name: [flag, ...] }

    If the user file sets extends_system = true, user groups are merged onto
    system groups (user wins per key). Otherwise the first found file wins.
    Returns an empty dict if no file is found (non-fatal).
    """

    def _load(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    paths = conflict_group_paths if conflict_group_paths is not None else CONFIG_PATHS
    user_path = paths[0] if len(paths) > 0 else None
    system_path = paths[1] if len(paths) > 1 else None

    user_data = _load(user_path) if user_path and user_path.exists() else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        return {}

    system_groups = system_data.get("append_conflict_groups", {}) if system_data else {}

    if user_data is None:
        _log.debug(f"Conflict groups (system):\n{pprint.pformat(system_groups, indent=2, sort_dicts=False)}")
        return system_groups

    user_groups = user_data.get("append_conflict_groups", {})

    if user_data.get("extends_system", False):
        merged = dict(system_groups)
        merged.update(user_groups)
        _log.debug(f"Conflict groups (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _log.debug(f"Conflict groups (user):\n{pprint.pformat(user_groups, indent=2, sort_dicts=False)}")
    return user_groups


def load_consumes_inference(paths=None):
    """
    Load [consumes_inference] from profiles.toml (user, then system).
    Returns dict: { makedep_tool: [conf_type, ...] }

    The inference map is system-defined and user-extendable. If the user file
    sets extends_system = true, user entries are merged onto system entries
    (user wins per key). Otherwise the first found file wins.
    Returns a built-in minimal default if no file is found.
    """

    def _load(p):
        with open(p, "rb") as f:
            return tomllib.load(f)

    _DEFAULT_INFERENCE = {
        "cargo":      ["makepkg", "rust", "env"],
        "rust":       ["makepkg", "rust", "env"],
        "rustup":     ["makepkg", "rust", "env"],
        "lib32-rust": ["makepkg", "rust", "env"],
        "meson":      ["makepkg", "meson", "env"],
        "cmake":      ["makepkg", "cmake", "env"],
        "ninja":      ["makepkg", "env"],
        "make":       ["makepkg", "env"],
        "python":     ["makepkg", "env"],
        "git":        ["makepkg"],
    }

    resolved_paths = paths if paths is not None else CONFIG_PATHS
    user_path   = resolved_paths[0] if len(resolved_paths) > 0 else None
    system_path = resolved_paths[1] if len(resolved_paths) > 1 else None

    user_data   = _load(user_path)   if user_path   and user_path.exists()   else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        _log.debug(f"Consumes inference (built-in defaults):\n{pprint.pformat(_DEFAULT_INFERENCE, indent=2, sort_dicts=False)}")
        return _DEFAULT_INFERENCE

    system_map = system_data.get("consumes_inference", {}) if system_data else {}

    if user_data is None:
        _log.info(f"Loaded consumes_inference: {system_path}")
        _log.debug(f"Consumes inference (system):\n{pprint.pformat(system_map, indent=2, sort_dicts=False)}")
        return system_map

    user_map = user_data.get("consumes_inference", {})

    if user_data.get("extends_system", False):
        merged = dict(system_map)
        merged.update(user_map)
        _log.info("Merged consumes_inference (user onto system)")
        _log.debug(f"Consumes inference (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _log.info(f"Loaded consumes_inference (user only): {user_path}")
    _log.debug(f"Consumes inference (user):\n{pprint.pformat(user_map, indent=2, sort_dicts=False)}")
    return user_map


# ---------------------------------------------------------------------------
# System makepkg.conf parsing
# ---------------------------------------------------------------------------

import re as _re

# Matches bash variable assignments in makepkg.conf:
#   KEY="value"   KEY='value'   KEY=bare   KEY=(array items)
#   export KEY=...
_MAKEPKG_ASSIGN_RE = _re.compile(
    r"""^[ \t]*(?:export[ \t]+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*(?P<value>.*)""",
    _re.MULTILINE,
)


def _parse_one_makepkg_conf(path: Path) -> dict:
    """Parse a single makepkg.conf file into {key: raw_value_string}."""
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except PermissionError:
        _log.warn(f"Cannot read {path} — skipping")
        return {}

    result = {}
    for m in _MAKEPKG_ASSIGN_RE.finditer(text):
        key = m.group("key")
        value = m.group("value").rstrip()

        # Backslash continuation: join lines ending with '\'
        if value.endswith("\\"):
            rest = text[m.end():]
            parts = [value[:-1]]
            for cont_line in rest.splitlines():
                stripped = cont_line.rstrip()
                if not stripped:
                    continue
                if stripped.endswith("\\"):
                    parts.append(stripped[:-1])
                else:
                    parts.append(stripped)
                    break
            value = "".join(parts)

        # Multiline array: if the value opens a '(' that isn't closed on the
        # same line, consume subsequent lines until paren depth reaches zero.
        if value.startswith("("):
            depth = value.count("(") - value.count(")")
            if depth > 0:
                rest = text[m.end():]
                extra = []
                for line in rest.splitlines():
                    extra.append(line)
                    depth += line.count("(") - line.count(")")
                    if depth <= 0:
                        break
                value = value + "\n" + "\n".join(extra)

        result[key] = value

    _log.info(f"Parsed {len(result)} keys from {path}")
    _log.debug("System makepkg.conf key=value pairs:\n" +
               "\n".join(f"  {k}={v}" for k, v in result.items()))
    return result


def _rewrite_makepkg_conf_text(text: str, mapping: dict[str, str]) -> str:
    """Return ``text`` with each ``KEY`` in ``mapping`` set to its value.

    An existing active ``KEY=...`` assignment is replaced in place; otherwise a
    commented ``#KEY=...`` line is uncommented and set; otherwise the
    assignment is appended. Values are written quoted (``KEY="value"``). All
    other lines are preserved verbatim.
    """
    lines = text.splitlines()
    remaining = dict(mapping)

    active_re = {
        k: _re.compile(rf"^([ \t]*)(?:export[ \t]+)?{_re.escape(k)}[ \t]*=")
        for k in mapping
    }
    commented_re = {
        k: _re.compile(rf"^([ \t]*)#[ \t]*(?:export[ \t]+)?{_re.escape(k)}[ \t]*=")
        for k in mapping
    }

    # Pass 1: replace active assignments.
    for i, line in enumerate(lines):
        for key in list(remaining):
            m = active_re[key].match(line)
            if m:
                lines[i] = f'{m.group(1)}{key}="{remaining.pop(key)}"'
                break

    # Pass 2: uncomment-and-set a commented assignment for any key still unset.
    for i, line in enumerate(lines):
        for key in list(remaining):
            m = commented_re[key].match(line)
            if m:
                lines[i] = f'{m.group(1)}{key}="{remaining.pop(key)}"'
                break

    # Pass 3: append anything still unset.
    if remaining:
        if lines and lines[-1].strip():
            lines.append("")
        for key, value in remaining.items():
            lines.append(f'{key}="{value}"')

    return "\n".join(lines) + "\n"


def set_makepkg_conf_keys(path, mapping: dict[str, str], dest=None) -> str:
    """Read ``path``, set each key in ``mapping``, write the result to ``dest``.

    ``dest`` defaults to ``path`` (in-place). Passing a separate ``dest`` lets a
    caller read a root-owned ``/etc/makepkg.conf`` and stage the rewrite to a
    user-writable temp file for a later ``sudo cp``. Returns the rewritten text.
    """
    path = Path(path)
    dest = Path(dest) if dest is not None else path
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new_text = _rewrite_makepkg_conf_text(text, mapping)
    dest.write_text(new_text, encoding="utf-8")
    return new_text


def _rewrite_profiles_default_toolchain(text: str, compiler: str) -> str:
    """Return ``text`` with ``[defaults] toolchain`` set to ``compiler``.

    Section-aware and comment-preserving (this is the runtime counterpart to the
    dev-only tomlkit path in tools/sync_config.py — no tomlkit at runtime):

      * An existing active or commented ``toolchain = ...`` inside the
        ``[defaults]`` section is replaced/uncommented in place.
      * If ``[defaults]`` exists without a ``toolchain`` key, the assignment is
        inserted directly after the ``[defaults]`` header.
      * If there is no ``[defaults]`` section, one is appended at end of file.

    All other lines (comments, other keys, other sections) are preserved
    verbatim. Only the *first* ``[defaults]`` table is touched.
    """
    lines = text.splitlines()
    new_line = f'toolchain = "{compiler}"'
    section_re = _re.compile(r"^\s*\[")
    defaults_re = _re.compile(r"^\s*\[defaults\]\s*(?:#.*)?$")
    key_re = _re.compile(r"^(\s*)#?\s*toolchain\s*=")

    trailing_nl = "\n" if text.endswith("\n") else ""

    defaults_idx = next(
        (i for i, line in enumerate(lines) if defaults_re.match(line)), None
    )

    if defaults_idx is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("[defaults]")
        lines.append(new_line)
        return "\n".join(lines) + "\n"

    # Scan the section body until the next table header.
    for j in range(defaults_idx + 1, len(lines)):
        if section_re.match(lines[j]):
            break
        m = key_re.match(lines[j])
        if m:
            lines[j] = f"{m.group(1)}{new_line}"
            return "\n".join(lines) + trailing_nl
    else:
        j = len(lines)
    # No toolchain key in [defaults] — insert right after the header.
    lines.insert(defaults_idx + 1, new_line)
    return "\n".join(lines) + trailing_nl


def _active_profiles_path() -> Path:
    """Return the profiles.toml the resolver actually reads (write target).

    Mirrors ``load_config``'s search: the first existing path in
    ``CONFIG_PATHS`` (user before system); when none exist yet, the system
    install path (``CONFIG_PATHS[-1]``).
    """
    for p in CONFIG_PATHS:
        if Path(p).exists():
            return Path(p)
    return Path(CONFIG_PATHS[-1])


def set_default_toolchain(compiler: str, path=None) -> str:
    """Write ``[defaults] toolchain = <compiler>`` to the live profiles.toml.

    The sole home for keeping ``profiles.toml``'s package-compiler default in
    sync with ``toolchain.toml``'s ``compiler``: the toolchain stage calls this
    on a successful register/build so the profile default tracks the toolchain
    it just registered. ``path`` defaults to :func:`_active_profiles_path`
    (the file the resolver reads); pass an explicit path in tests to avoid
    mutating the committed fixture. Returns the rewritten text.
    """
    path = Path(path) if path is not None else _active_profiles_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        text = ""
    new_text = _rewrite_profiles_default_toolchain(text, compiler)
    path.write_text(new_text, encoding="utf-8")
    _log.info(f"Set [defaults] toolchain = {compiler!r} in {path}")
    return new_text


def parse_system_makepkg_conf(path=None):
    """
    Parse makepkg.conf into a dict of {key: raw_value_string}.

    raw_value_string is the verbatim value text as it appears in the file
    (including surrounding quotes or parentheses) so it can be written back
    into a new conf file unchanged.

    When path is None, mirrors makepkg's own conf layering:
      1. /etc/makepkg.conf  (system baseline)
      2. $XDG_CONFIG_HOME/pacman/makepkg.conf  (user override, XDG path)
      3. ~/.makepkg.conf  (user override, legacy path)
    User conf keys override system conf keys.  An explicit path skips layering.

    Returns empty dict if no conf is found or readable.
    """
    import os as _os

    if path is not None:
        result = _parse_one_makepkg_conf(Path(path))
        if not result:
            _log.warn(f"makepkg.conf not found or unreadable at {path} — will use profile values only")
        return result

    # Layer system conf then user conf(s), later entries win.
    result = _parse_one_makepkg_conf(Path("/etc/makepkg.conf"))
    if not result:
        _log.warn("System makepkg.conf not found at /etc/makepkg.conf — will use profile values only")

    xdg_config = _os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    user_conf_paths = [
        Path(xdg_config) / "pacman" / "makepkg.conf",
        Path.home() / ".makepkg.conf",
    ]
    for user_path in user_conf_paths:
        user_keys = _parse_one_makepkg_conf(user_path)
        if user_keys:
            _log.info(f"Merging user makepkg.conf: {user_path} ({len(user_keys)} keys)")
            result.update(user_keys)

    return result
