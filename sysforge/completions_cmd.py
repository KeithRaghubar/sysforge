"""
completions_cmd.py — ``sysforge completions`` verb.

A shell-completion data sink: prints candidate names for a requested resource
(``makepkg-flags`` / ``state`` / ``manifest`` / ``local`` / the default
repo+AUR package universe), one per line, for the ``_sysforge`` completion
script to consume. Not user-facing. Dispatched through the Verb framework; the
argparse surface lives in ``cli._build_parser``.
"""
from pathlib import Path

from sysforge.primitives.config import expand_package_groups, load_config
from sysforge.primitives.paths import resolve_packages_path
from sysforge.verbs import ExecResult, PreCheckResult, Verb


class CompletionsVerb(Verb):
    """Shell-completion data sink. Not user-facing; called from _sysforge."""

    name = "completions"
    requires_sentinel = False

    def pre_check(self, args) -> PreCheckResult:
        return PreCheckResult()

    def execute(self, args, pre: PreCheckResult) -> ExecResult:
        import subprocess as _sp
        config = load_config() or {}

        if args.resource == "makepkg-flags":
            r = _sp.run(["makepkg", "--help"], capture_output=True, text=True)
            text = r.stdout or r.stderr or ""
            _exclude = {"-h", "--help", "-V", "--version", "-p", "-m", "--nocolor"}
            import re
            for line in text.splitlines():
                m = re.match(r"^\s+(-\w),\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
                if m:
                    short, long, desc = m.group(1), m.group(2), m.group(3).strip()
                    if short not in _exclude:
                        print(f"{short}:{desc}")
                    if long not in _exclude:
                        print(f"{long}:{desc}")
                    continue
                m = re.match(r"^\s+(--\w[\w-]*)\s+(?:<\w+>\s+)?(.*)", line)
                if m:
                    long, desc = m.group(1), m.group(2).strip()
                    if long not in _exclude:
                        print(f"{long}:{desc}")
            return ExecResult()

        if args.resource == "state":
            from sysforge.pipeline.state import resolve_state_dir
            from sysforge.primitives.build_state import BuildState
            state_dir, _ = resolve_state_dir(None)
            bs = BuildState(state_dir)
            for name in sorted(bs.all_packages()):
                print(name)
            return ExecResult()

        if args.resource == "manifest":
            import tomllib as _tomllib
            pkg_path = resolve_packages_path(config)
            if pkg_path.exists():
                with open(pkg_path, "rb") as _f:
                    data = _tomllib.load(_f)
                for entry in expand_package_groups(data):
                    name = entry.get("name")
                    if name:
                        print(name)
            return ExecResult()

        if args.resource == "local":
            raw = config.get("paths", {}).get("pkgbuild_src_dir")
            if raw:
                d = Path(raw).expanduser()
                if d.is_dir():
                    for sub in sorted(d.iterdir()):
                        if sub.is_dir() and (sub / "PKGBUILD").exists():
                            print(sub.name)
            return ExecResult()

        seen: set[str] = set()
        raw = config.get("paths", {}).get("pkgbuild_src_dir")
        if raw:
            d = Path(raw).expanduser()
            if d.is_dir():
                for sub in sorted(d.iterdir()):
                    if sub.is_dir() and (sub / "PKGBUILD").exists():
                        if sub.name not in seen:
                            seen.add(sub.name)
                            print(sub.name)

        r = _sp.run(["pacman", "-Ssq"], capture_output=True, text=True)
        if r.returncode == 0:
            for name in r.stdout.splitlines():
                if name and name not in seen:
                    seen.add(name)
                    print(name)

        from sysforge.primitives.aur import AUR_CACHE_PATH
        aur_cache = AUR_CACHE_PATH.expanduser()
        if aur_cache.exists():
            for name in aur_cache.read_text().splitlines():
                if name and name not in seen:
                    seen.add(name)
                    print(name)
        return ExecResult()
