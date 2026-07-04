# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/_bootstrap.py — shared bootstrap config loader for stages 1-3.

Reads /etc/sysforge/bootstrap.toml (or a given path) and returns a
BootstrapConfig dataclass used by the install, hardware, and configure
stages (and the archinstall config builder they feed).
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sysforge import log
from sysforge.primitives.paths import BOOTSTRAP_PATH
from sysforge.primitives.pkg_catalog import valid_desktops

_log = log.get_logger("BOOTSTRAP")

_VALID_ROOT_FS = {"ext4", "btrfs"}
_VALID_SHELLS  = {"bash", "zsh"}
_ZONEINFO_DIR  = Path("/usr/share/zoneinfo")


@dataclass
class BootstrapConfig:
    target: str
    device: str
    hostname: str
    locale: str
    timezone: str
    esp_size_mib: int = 512
    root_fs: str = "ext4"
    keymap: str = "us"
    parallel_downloads: int = 5
    mirror_countries: list[str] = field(default_factory=list)
    mirror_protocol: str = "https"
    mirror_age: int = 12
    root_password: str | None = None
    username: str = "builder"
    user_password: str | None = None
    shell: str = "bash"
    desktop: str | None = None
    makepkg_packager: str | None = None
    makepkg_makeflags: str | None = None


def load_bootstrap(path: Path | None = None) -> BootstrapConfig:
    """
    Load and validate bootstrap.toml.

    Raises RuntimeError on missing file, parse error, missing required
    fields, or invalid values.
    """
    p = Path(path) if path is not None else BOOTSTRAP_PATH

    if not p.exists():
        raise RuntimeError(
            f"bootstrap.toml not found at {p}. "
            f"Create it before running the bootstrap pipeline."
        )

    try:
        data = tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"bootstrap.toml parse error: {exc}") from exc

    partition = data.get("partition", {})
    system = data.get("system", {})
    mirror = data.get("mirror", {})
    desktop_tbl = data.get("desktop", {})
    makepkg_tbl = data.get("makepkg", {})

    # Required fields
    def _require(section: dict, key: str, section_name: str) -> str:
        if key not in section:
            raise RuntimeError(
                f"bootstrap.toml missing required field [{section_name}].{key}"
                if section_name else
                f"bootstrap.toml missing required field: {key}"
            )
        return str(section[key])

    target = _require(data, "target", "")
    device = _require(partition, "device", "partition")
    hostname = _require(system, "hostname", "system")
    locale = _require(system, "locale", "system")
    timezone = _require(system, "timezone", "system")

    # Optional with defaults
    esp_size_mib = partition.get("esp_size_mib", 512)
    root_fs = partition.get("root_fs", "ext4")
    keymap = system.get("keymap", "us")
    parallel_downloads = system.get("parallel_downloads", 5)
    mirror_countries = mirror.get("countries", [])
    mirror_protocol = mirror.get("protocol", "https")
    mirror_age = mirror.get("age", 12)
    root_password = system.get("root_password") or None
    raw_username = system.get("username")
    if not raw_username:
        _log.info("[system].username not set in bootstrap.toml — defaulting to 'builder'")
    username = raw_username or "builder"
    user_password = system.get("user_password") or None
    shell = system.get("shell", "bash")
    desktop = desktop_tbl.get("environment") or None
    makepkg_packager = makepkg_tbl.get("packager") or None
    makepkg_makeflags = makepkg_tbl.get("makeflags") or None

    if root_fs not in _VALID_ROOT_FS:
        raise RuntimeError(
            f"bootstrap.toml: invalid root_fs {root_fs!r}. "
            f"Supported values: {', '.join(sorted(_VALID_ROOT_FS))}."
        )

    if shell not in _VALID_SHELLS:
        raise RuntimeError(
            f"bootstrap.toml: invalid shell {shell!r}. "
            f"Supported values: {', '.join(sorted(_VALID_SHELLS))}."
        )

    if desktop is not None and desktop not in valid_desktops():
        raise RuntimeError(
            f"bootstrap.toml: invalid [desktop].environment {desktop!r}. "
            f"Supported values: {', '.join(valid_desktops())}."
        )

    if _ZONEINFO_DIR.exists() and not (_ZONEINFO_DIR / timezone).exists():
        raise RuntimeError(
            f"bootstrap.toml: invalid timezone {timezone!r}. "
            f"Check /usr/share/zoneinfo/ for valid values "
            f"(e.g. 'America/Toronto', 'Europe/London', 'UTC')."
        )

    for country in mirror_countries:
        if not isinstance(country, str) or not country.strip():
            raise RuntimeError(
                "bootstrap.toml: [mirror].countries entries must be non-empty strings."
            )

    return BootstrapConfig(
        target=target,
        device=device,
        hostname=hostname,
        locale=locale,
        timezone=timezone,
        esp_size_mib=esp_size_mib,
        root_fs=root_fs,
        keymap=keymap,
        parallel_downloads=parallel_downloads,
        mirror_countries=mirror_countries,
        mirror_protocol=mirror_protocol,
        mirror_age=mirror_age,
        root_password=root_password,
        username=username,
        user_password=user_password,
        shell=shell,
        desktop=desktop,
        makepkg_packager=makepkg_packager,
        makepkg_makeflags=makepkg_makeflags,
    )
