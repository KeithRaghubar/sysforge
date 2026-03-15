"""
config.py — SysForge config file loading

Responsible for all TOML config file I/O: flag profiles, conflict groups,
and consumes inference. Owns the path constants and user/system merge logic.

Public API:
    load_config(config_paths=None)         -> dict
    load_conflict_groups(paths=None)       -> dict
    load_consumes_inference(paths=None)    -> dict
    find_pkgbuild(pkg, config=None)        -> Path
"""
import os
import pprint
import tomllib
import sysforge.log as _log
from pathlib import Path


CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

CONFIG_PATHS = [
    Path.home() / ".config/sysforge/flag_profiles.toml",
    CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
]

CONFLICT_GROUP_PATHS = [
    Path.home() / ".config/sysforge/append_conflict_groups.toml",
    CONFIG_BASE / "etc/sysforge/append_conflict_groups.toml",
]

CONSUMES_INFERENCE_PATHS = [
    Path.home() / ".config/sysforge/consumes_inference.toml",
    CONFIG_BASE / "etc/sysforge/consumes_inference.toml",
]

PACKAGES_PATH = CONFIG_BASE / "etc/sysforge/packages.toml"


def find_pkgbuild(pkg: str, config: dict | None = None) -> Path:
    """
    Resolve a PKGBUILD path from a pkg argument.

    Search order:
    1. pkg is an existing path → use directly.
    2. <cwd>/<pkg>/PKGBUILD
    3. <config [paths] pkgbuild_dir>/<pkg>/PKGBUILD  (if configured)

    Raises FileNotFoundError listing all searched paths if nothing is found.
    """
    p = Path(pkg)
    if p.exists():
        return p.resolve()

    searched: list[Path] = [p]

    cwd_candidate = Path.cwd() / pkg / "PKGBUILD"
    searched.append(cwd_candidate)
    if cwd_candidate.exists():
        return cwd_candidate.resolve()

    if config:
        raw = config.get("paths", {}).get("pkgbuild_dir")
        if raw:
            dir_candidate = Path(raw).expanduser() / pkg / "PKGBUILD"
            searched.append(dir_candidate)
            if dir_candidate.exists():
                return dir_candidate.resolve()

    searched_str = "\n".join(f"    {s}" for s in searched)
    raise FileNotFoundError(
        f"PKGBUILD not found for {pkg!r}.\n"
        f"  Searched:\n{searched_str}\n"
        f"  Pass a full path, set [paths] pkgbuild_dir in flag_profiles.toml,\n"
        f"  or cd into the package directory and run: sysforge build/resolve <name>"
    )


def load_config(config_paths=None):
    """
    Load flag_profiles.toml from CONFIG_PATHS (user, then system).
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
            f"[CONFIG] No flag_profiles.toml found. Searched:\n"
            + "\n".join(f"  {p}" for p in CONFIG_PATHS)
        )

    if user_config is None:
        _validate_rule_priorities(system_config.get("rules", []), "system")
        _log.info("[CONFIG]", f"Loaded system config: {system_path}")
        _log.debug("[CONFIG]", f"Full flag_profiles (system):\n{pprint.pformat(system_config, indent=2, sort_dicts=False)}")
        return system_config

    if system_config is None:
        _validate_rule_priorities(user_config.get("rules", []), "user")
        _log.info("[CONFIG]", f"Loaded user config: {user_path}")
        _log.debug("[CONFIG]", f"Full flag_profiles (user):\n{pprint.pformat(user_config, indent=2, sort_dicts=False)}")
        return user_config

    if user_config.get("extends_system", False):
        _log.info("[CONFIG]", "Merging user config onto system config")

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
        _log.debug("[CONFIG]", f"Full flag_profiles (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _validate_rule_priorities(user_config.get("rules", []), "user")
    _log.info("[CONFIG]", f"User config overrides system config: {user_path}")
    _log.debug("[CONFIG]", f"Full flag_profiles (user overrides system):\n{pprint.pformat(user_config, indent=2, sort_dicts=False)}")
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
    Load append_conflict_groups.toml from user and system paths.
    Returns dict: { group_name: [flag, ...] }

    If the user file sets extends_system = true, user groups are merged onto
    system groups (user wins per key). Otherwise the first found file wins.
    Returns an empty dict if no file is found (non-fatal).
    """

    def _load(path):
        with open(path, "rb") as f:
            return tomllib.load(f)

    paths = conflict_group_paths if conflict_group_paths is not None else CONFLICT_GROUP_PATHS
    user_path = paths[0] if len(paths) > 0 else None
    system_path = paths[1] if len(paths) > 1 else None

    user_data = _load(user_path) if user_path and user_path.exists() else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        return {}

    system_groups = system_data.get("conflict_groups", {}) if system_data else {}

    if user_data is None:
        _log.debug("[CONFIG]", f"Conflict groups (system):\n{pprint.pformat(system_groups, indent=2, sort_dicts=False)}")
        return system_groups

    user_groups = user_data.get("conflict_groups", {})

    if user_data.get("extends_system", False):
        merged = dict(system_groups)
        merged.update(user_groups)
        _log.debug("[CONFIG]", f"Conflict groups (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _log.debug("[CONFIG]", f"Conflict groups (user):\n{pprint.pformat(user_groups, indent=2, sort_dicts=False)}")
    return user_groups


def load_consumes_inference(paths=None):
    """
    Load consumes_inference.toml from system (and optionally user) paths.
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
        "cargo":  ["makepkg", "rust", "env"],
        "meson":  ["makepkg", "meson", "env"],
        "cmake":  ["makepkg", "cmake", "env"],
        "ninja":  ["makepkg", "env"],
        "make":   ["makepkg", "env"],
        "python": ["makepkg", "env"],
        "git":    ["makepkg"],
    }

    resolved_paths = paths if paths is not None else CONSUMES_INFERENCE_PATHS
    user_path   = resolved_paths[0] if len(resolved_paths) > 0 else None
    system_path = resolved_paths[1] if len(resolved_paths) > 1 else None

    user_data   = _load(user_path)   if user_path   and user_path.exists()   else None
    system_data = _load(system_path) if system_path and system_path.exists() else None

    if user_data is None and system_data is None:
        _log.debug("[CONFIG]", f"Consumes inference (built-in defaults):\n{pprint.pformat(_DEFAULT_INFERENCE, indent=2, sort_dicts=False)}")
        return _DEFAULT_INFERENCE

    system_map = system_data.get("consumes_inference", {}) if system_data else {}

    if user_data is None:
        _log.info("[CONFIG]", f"Loaded consumes_inference: {system_path}")
        _log.debug("[CONFIG]", f"Consumes inference (system):\n{pprint.pformat(system_map, indent=2, sort_dicts=False)}")
        return system_map

    user_map = user_data.get("consumes_inference", {})

    if user_data.get("extends_system", False):
        merged = dict(system_map)
        merged.update(user_map)
        _log.info("[CONFIG]", "Merged consumes_inference (user onto system)")
        _log.debug("[CONFIG]", f"Consumes inference (merged):\n{pprint.pformat(merged, indent=2, sort_dicts=False)}")
        return merged

    _log.info("[CONFIG]", f"Loaded consumes_inference (user only): {user_path}")
    _log.debug("[CONFIG]", f"Consumes inference (user):\n{pprint.pformat(user_map, indent=2, sort_dicts=False)}")
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


def parse_system_makepkg_conf(path=None):
    """
    Parse /etc/makepkg.conf (or the given path) into a dict of
    {key: raw_value_string}.

    raw_value_string is the verbatim value text as it appears in the file
    (including surrounding quotes or parentheses) so it can be written back
    into a new conf file unchanged.

    Returns empty dict if the file is not found or unreadable.
    """
    if path is None:
        path = Path("/etc/makepkg.conf")
    else:
        path = Path(path)

    if not path.exists():
        _log.warn("[CONFIG]", f"System makepkg.conf not found at {path} — will use profile values only")
        return {}

    try:
        text = path.read_text()
    except PermissionError:
        _log.warn("[CONFIG]", f"Cannot read {path} — will use profile values only")
        return {}

    result = {}
    for m in _MAKEPKG_ASSIGN_RE.finditer(text):
        key = m.group("key")
        value = m.group("value").rstrip()

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

    _log.info("[CONFIG]", f"Parsed {len(result)} keys from {path}")
    _log.debug("[CONFIG]", f"System makepkg.conf key=value pairs:\n" +
               "\n".join(f"  {k}={v}" for k, v in result.items()))
    return result
