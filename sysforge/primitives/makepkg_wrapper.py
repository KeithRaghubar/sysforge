import os
import sys
import tempfile
import subprocess
import tomllib
from pathlib import Path

from sysforge.primitives.pkgbuild_meta import parse_pkgbuild

CONFIG_BASE = Path(os.environ.get("SYSFORGE_CONFIG_DIR", "/"))

CONFIG_PATHS = [
    Path.home() / ".config/sysforge/flag_profiles.toml",
    CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
]


def load_config():
    pass


def resolve_profile(pkgmeta, config):
    pass


def merge_extends(profile_name, profiles, visited=None):
    pass


def match_rules(pkgmeta, rules):
    pass


def resolve_groups(pkgmeta, matched_rules, defaults):
    pass


def emit_makepkg_conf(resolved_profile):
    pass


def invoke_makepkg(pkgbuild_path, conf_path):
    pass


def run(pkgbuild_path):
    pkgmeta = parse_pkgbuild(pkgbuild_path)
    config = load_config()

    resolved_profile = resolve_profile(pkgmeta, config)
    groups = resolve_groups(pkgmeta, resolved_profile, config.get("defaults", {}))

    build_mode = resolved_profile.get("build_mode", None)

    if build_mode == "pgo_llvm_toolchain":
        pass  # hand off to pgo handler
    elif build_mode == "patch_linker":
        pass  # hand off to linker patcher
    else:
        with emit_makepkg_conf(resolved_profile) as conf_path:
            invoke_makepkg(pkgbuild_path, conf_path)


if __name__ == "__main__":
    run(sys.argv[1])
