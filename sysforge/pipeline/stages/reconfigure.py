"""
stages/reconfigure.py — stage 5: pre-build checkpoint

Runs after bootstrap configuration and before the build stages
(toolchain → packages → kernel). Safe to re-run on a live system at any time.

Presents a menu of available checks. The user can run all, a numbered subset,
a range, or refer to steps by name. Defaults to all when non-interactive.

Available steps:
  1  editor      Editor selection (SYSFORGE_EDITOR → sysforge.toml → $EDITOR → vi)
  2  config      Config file review (flag_profiles, packages, toolchain, kernel, hardware_profile)
  3  build_mode  View/set packages.toml repo_mode (pacman | profiled)
  4  makepkg     System makepkg.conf review (MAKEFLAGS, BUILDDIR, PKGDEST, flags)
  5  sudo        User / sudo verification
  6  disk        Disk space check
  7  network     Network connectivity probe (AUR, GitHub, mirrors)
  8  gpg         GPG keyring bootstrap (global key store import, refresh option)
  9  preview     Build preview (tentative profiles for all packages.toml entries)

Non-interactive / dry_run:
  All steps run without prompting. dry_run additionally skips writes
  (no file edits, no sysforge.toml updates, no GPG imports).
"""
import os
import re
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

from sysforge import log
_log = log.get_logger("RECONFIGURE")
from sysforge.pipeline.stages.base import BootstrapRebootRequired, Stage
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives.config import (
    load_config,
    load_conflict_groups,
)
from sysforge.primitives.paths import (
    CONFIG_BASE,
    KERNEL_PATH,
    PACKAGES_PATH,
    SYSFORGE_TOML_PATH,
    TOOLCHAIN_PATH,
    resolve_packages_path,
)

# Hard-coded to avoid circular import from stages/__init__.py
_PIPELINE_STAGES = [
    ("partition",    "Disk partitioning"),
    ("base_install", "Base system install"),
    ("hardware",     "Hardware detection"),
    ("configure",    "Bootstrap configuration"),
    ("reconfigure",  "Pre-build checkpoint"),
    ("toolchain",    "LLVM toolchain build"),
    ("packages",     "Build and install packages"),
    ("kernel",       "Build and install custom kernel"),
]

# Ordered step definitions: (key, short_label, description)
_STEPS = [
    ("editor",     "Editor selection",
     "Set preferred editor; save permanently to sysforge.toml"),
    ("config",     "Config file review",
     "Review flag_profiles.toml, packages.toml, toolchain.toml, kernel.toml, hardware_profile.toml"),
    ("build_mode", "Build mode",
     "View/set packages.toml repo_mode (pacman | profiled); show per-package pkgbuild_patch overrides"),
    ("makepkg",    "makepkg.conf review",
     "Review /etc/makepkg.conf — MAKEFLAGS, BUILDDIR, PKGDEST, flag baselines"),
    ("sudo",       "User / sudo verification",
     "Confirm build user and sudoers are correctly configured for makepkg -si"),
    ("disk",       "Disk space check",
     "Estimate required build space and warn if free space is low"),
    ("network",    "Network probe",
     "Verify AUR, GitHub, Arch Linux, and pacman mirrors are reachable"),
    ("gpg",        "GPG keyring bootstrap",
     "Import global key store, report keyring status, offer keyserver refresh"),
    ("preview",    "Build preview",
     "Show what packages + kernel stages will build, with tentative profiles"),
]

_STEP_KEYS  = [k for k, _, _ in _STEPS]

_DISK_WARN_GB    = 20
_DISK_PER_PKG_GB = 3
_MAKEPKG_CONF_HIGHLIGHT = [
    "MAKEFLAGS", "BUILDDIR", "PKGDEST", "PKGEXT",
    "CFLAGS", "CXXFLAGS", "LDFLAGS",
]


# ---------------------------------------------------------------------------
# Helpers: sysforge.toml
# ---------------------------------------------------------------------------

def _load_sysforge_toml() -> dict:
    if not SYSFORGE_TOML_PATH.exists():
        return {}
    try:
        with open(SYSFORGE_TOML_PATH, "rb") as f:
            return tomllib.load(f)
    except Exception:
        return {}


def _save_sysforge_toml_ui(key: str, value: str) -> None:
    data = _load_sysforge_toml()
    ui = dict(data.get("ui", {}))
    ui[key] = value
    data["ui"] = ui

    lines = []
    for section, settings in data.items():
        lines.append(f"[{section}]")
        for k, v in settings.items():
            if isinstance(v, bool):
                lines.append(f"{k} = {'true' if v else 'false'}")
            elif isinstance(v, str):
                lines.append(f'{k} = "{v}"')
            else:
                lines.append(f"{k} = {v}")
        lines.append("")

    SYSFORGE_TOML_PATH.parent.mkdir(parents=True, exist_ok=True)
    SYSFORGE_TOML_PATH.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Helpers: interactivity
# ---------------------------------------------------------------------------

def _interactive() -> bool:
    return sys.stdin.isatty()


def _prompt(msg: str, default: str = "") -> str:
    try:
        return input(msg).strip()
    except EOFError:
        return default


# ---------------------------------------------------------------------------
# Stage progress summary
# ---------------------------------------------------------------------------

def _show_stage_summary(state) -> None:
    _log.ui("─── Pipeline progress ───────────────────────────────")
    _symbols = {
        "done":       "✓",
        "skipped_to": "↷",
        "running":    "→",
        "failed":     "✗",
    }
    for name, desc in _PIPELINE_STAGES:
        status = state.stage_status(name)
        if name == "reconfigure":
            symbol, label = "→", "running"
        else:
            symbol = _symbols.get(status, "·")
            label  = status or "pending"
        _log.ui(f"  {symbol}  {name:<16}  {label}")
    _log.ui("─────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# Menu: step selection
# ---------------------------------------------------------------------------

def _show_step_menu() -> None:
    _log.ui("─── Steps ───────────────────────────────────────────")
    _log.ui("  [0]  cancel     Skip all steps and exit")
    for i, (key, label, desc) in enumerate(_STEPS, 1):
        _log.ui(f"  [{i}]  {key:<10}  {label}")
        _log.ui(f"              {desc}")
    _log.ui("─────────────────────────────────────────────────────")


def _parse_step_selection(raw: str) -> list[str]:
    """
    Parse a step selection string into an ordered list of step keys.

    Accepts any combination of:
      - '' or 'all'        → all steps
      - '1 3 5'            → steps by number
      - '2-6'              → inclusive range
      - 'network gpg'      → steps by name
      - '1-3 gpg preview'  → mixed
    Returns all steps if nothing valid was parsed.
    """
    raw = raw.strip()
    if not raw or raw.lower() == "all":
        return list(_STEP_KEYS)
    if raw == "0" or raw.lower() == "cancel":
        return []

    selected: list[str] = []

    for token in raw.split():
        if token == "0":
            return []
        # Range: 2-6
        if re.match(r"^\d+-\d+$", token):
            start, end = (int(x) for x in token.split("-", 1))
            if 1 <= start <= end <= len(_STEP_KEYS):
                for i in range(start, end + 1):
                    key = _STEP_KEYS[i - 1]
                    if key not in selected:
                        selected.append(key)
        # Number
        elif token.isdigit():
            i = int(token)
            if 1 <= i <= len(_STEP_KEYS):
                key = _STEP_KEYS[i - 1]
                if key not in selected:
                    selected.append(key)
        # Name
        elif token in _STEP_KEYS:
            if token not in selected:
                selected.append(token)

    return selected if selected else list(_STEP_KEYS)


def _select_steps(options) -> list[str]:
    """Return ordered list of step keys to run for this invocation."""
    if not _interactive() or options.dry_run:
        return list(_STEP_KEYS)

    _show_step_menu()
    raw = _prompt(
        "[RECONFIGURE] Steps to run [↵ for all, or e.g. '1 3', '2-5', 'network gpg']: "
    )
    steps = _parse_step_selection(raw)

    if steps:
        _log.ui(f"Running steps: {', '.join(steps)}")
    else:
        _log.ui("Cancelled.")
    return steps


# ---------------------------------------------------------------------------
# Step: editor selection
# ---------------------------------------------------------------------------

def _resolve_editor() -> tuple[str, str]:
    sysforge_cfg = _load_sysforge_toml()
    candidates = [
        (os.environ.get("SYSFORGE_EDITOR"), "SYSFORGE_EDITOR"),
        (sysforge_cfg.get("ui", {}).get("editor"), "sysforge.toml"),
        (os.environ.get("EDITOR"), "$EDITOR"),
        (os.environ.get("VISUAL"), "$VISUAL"),
    ]
    for value, source in candidates:
        if value:
            return value, source
    for fallback in ("vim", "nano", "vi"):
        if shutil.which(fallback):
            return fallback, "detected"
    return "vi", "default"


def _step_editor(config, state, options, editor: str) -> str:
    """Show current editor, offer to change. Returns editor to use."""
    editor, source = _resolve_editor()
    _log.ui(f"─── Editor selection ────────────────────────────────")

    if not _interactive() or options.dry_run:
        return editor

    choice = _prompt(f"  Editor: {editor} (from {source}). Change? [e]dit / [↵] keep: ")
    if choice.lower() != "e":
        return editor

    new_editor = _prompt(f"  Enter editor command [{editor}]: ") or editor
    if new_editor == editor:
        return editor

    if not shutil.which(new_editor):
        _log.warn(f"  {new_editor!r} not found in PATH")
        install = _prompt(f"  Try 'pacman -S {new_editor}'? [y/N]: ").lower()
        if install == "y" and not options.dry_run:
            result = subprocess.run(
                ["sudo", "pacman", "-S", "--needed", "--noconfirm", new_editor]
            )
            if result.returncode != 0 or not shutil.which(new_editor):
                _log.warn(
                    f"  Install failed — keeping {editor!r}. "
                    f"Install {new_editor!r} manually and re-run the editor step."
                )
                return editor
            _log.ui(f"  {new_editor} installed")
        else:
            _log.warn(
                f"  Keeping {editor!r}. "
                f"Install {new_editor!r} manually and re-run the editor step to change."
            )
            return editor

    save = _prompt("  Save as sysforge default? [y/N]: ").lower()
    if save == "y":
        try:
            _save_sysforge_toml_ui("editor", new_editor)
            _log.ui(f"  Saved to {SYSFORGE_TOML_PATH}")
        except OSError as e:
            _log.warn(f"  Could not save preference: {e}")

    return new_editor


# ---------------------------------------------------------------------------
# Step: config file review
# ---------------------------------------------------------------------------

def _validate_flag_profiles(path: Path) -> tuple[bool, str]:
    try:
        with open(path, "rb") as f:
            tomllib.load(f)  # pre-validate TOML syntax
    except Exception as e:
        return False, f"TOML parse error: {e}"
    try:
        cfg = load_config(config_paths=[path])
        if cfg is None:
            return False, "load_config returned None"
        conflict_groups = load_conflict_groups()
        from sysforge.primitives.profile import merge_extends
        profiles = cfg.get("profiles", {})
        for name in profiles:
            merge_extends(name, profiles, conflict_groups=conflict_groups)
        return True, f"{len(profiles)} profiles, {len(cfg.get('rules', []))} rules"
    except Exception as e:
        return False, str(e)


def _open_in_editor(path: Path, editor: str) -> None:
    try:
        result = subprocess.run([editor, str(path)])
        if result.returncode != 0:
            _log.warn(f"  Editor exited non-zero for {path.name}")
    except FileNotFoundError:
        _log.warn(f"  Editor not found: {editor!r}")


def _review_config_file(
    label: str,
    path: Path,
    editor: str,
    dry_run: bool,
    validate_fn=None,
    warn: str | None = None,
) -> None:
    exists = path.exists()
    _log.ui(
        f"  {label}: {path}" if exists else f"  {label}: {path}  (not found — skipping)"
    )
    if not exists or not _interactive() or dry_run:
        return

    if _prompt(f"    {label} ({path.name}) — [e]dit / [↵] skip: ").lower() != "e":
        return

    if warn:
        _log.warn(f"    ⚠  {warn}")
        if _prompt("    Proceed? [y/N]: ").lower() != "y":
            return

    while True:
        _open_in_editor(path, editor)
        if validate_fn is None:
            break
        _log.ui(f"    Validating {label}...")
        ok, msg = validate_fn(path)
        if ok:
            _log.ui(f"    ✓ {msg}")
            break
        _log.warn(f"    ✗ {msg}")
        action = _prompt(
            "    [r]e-open in editor / [s]kip (keep previous) / [a]bort: "
        ).lower()
        if action == "r":
            continue
        elif action == "a":
            raise RuntimeError(f"[RECONFIGURE] Aborted at {label} validation failure")
        else:
            _log.warn("    Skipping — file may be invalid, proceeding with caution")
            break


def _step_config(config, state, options, editor: str) -> str:
    _log.ui("─── Config file review ──────────────────────────────")

    _review_config_file(
        "flag_profiles.toml",
        CONFIG_BASE / "etc/sysforge/flag_profiles.toml",
        editor, options.dry_run,
        validate_fn=_validate_flag_profiles,
    )
    _review_config_file(
        "packages.toml", PACKAGES_PATH, editor, options.dry_run,
    )

    if TOOLCHAIN_PATH.exists():
        _review_config_file("toolchain.toml", TOOLCHAIN_PATH, editor, options.dry_run)

    if KERNEL_PATH.exists():
        _review_config_file("kernel.toml", KERNEL_PATH, editor, options.dry_run)

    state_dir, _ = resolve_state_dir(None)
    hw_path = Path(config.get("hardware_profile") or state_dir / "hardware_profile.toml")
    if hw_path.exists():
        _review_config_file(
            "hardware_profile.toml", hw_path, editor, options.dry_run,
            warn="Machine-generated by the hardware stage. "
                 "Manual edits can cause driver mismatches or broken kconfig.",
        )
    return editor


# ---------------------------------------------------------------------------
# Step: build mode (repo_mode)
# ---------------------------------------------------------------------------

def _set_repo_mode(pkg_path: Path, mode: str) -> None:
    """
    Write repo_mode to the [build] section of packages.toml in-place,
    preserving all other content and comments.

    Search order:
    1. Replace existing repo_mode = "..." line.
    2. Insert after [build] header if not found.
    3. Append a new [build] section if none exists.
    """
    text = pkg_path.read_text()

    new_text, n = re.subn(
        r'^(repo_mode\s*=\s*)"[^"]*"',
        f'\\1"{mode}"',
        text,
        flags=re.MULTILINE,
    )
    if n:
        pkg_path.write_text(new_text)
        return

    # Insert immediately after [build] header
    new_text = re.sub(
        r'(\[build\][^\n]*\n)',
        f'\\1repo_mode = "{mode}"\n',
        text,
        count=1,
    )
    if new_text != text:
        pkg_path.write_text(new_text)
        return

    # No [build] section — append one
    pkg_path.write_text(text.rstrip("\n") + f'\n\n[build]\nrepo_mode = "{mode}"\n')


def _step_build_mode(config, state, options, editor: str) -> str:
    _log.ui("─── Build mode ──────────────────────────────────────")

    pkg_path = resolve_packages_path(config)
    if not pkg_path.exists():
        _log.ui(f"  packages.toml not found at {pkg_path} — skipping")
        return editor

    try:
        with open(pkg_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        _log.warn(f"  Could not load packages.toml: {e}")
        return editor

    build_cfg = data.get("build", {})
    repo_mode = build_cfg.get("repo_mode", "pacman")
    packages  = data.get("package", [])
    patched   = [p["name"] for p in packages if p.get("pkgbuild_patch")]

    _log.ui(f"  File:       {pkg_path}")
    _log.ui(f"  repo_mode:  {repo_mode}")
    _log.ui("  pacman    — repo packages installed via pacman (no profiled flags)")
    _log.ui("  profiled  — repo packages cloned and built from source with profile flags")

    if patched:
        _log.ui(
            f"  Per-package pkgbuild_patch overrides ({len(patched)}): "
            + ", ".join(patched)
        )
    else:
        _log.ui("  No per-package pkgbuild_patch overrides.")

    if not _interactive() or options.dry_run:
        return editor

    choice = _prompt(
        f"  Change repo_mode from {repo_mode!r}? [p]acman / [r]profiled / [↵] keep: "
    ).lower()

    if choice == "p":
        new_mode = "pacman"
    elif choice in ("r", "profiled"):
        new_mode = "profiled"
    else:
        return editor

    if new_mode == repo_mode:
        _log.ui(f"  Already {repo_mode!r} — no change.")
        return editor

    try:
        _set_repo_mode(pkg_path, new_mode)
        _log.ui(f"  repo_mode set to {new_mode!r} in {pkg_path}")
    except OSError as e:
        _log.warn(f"  Could not write {pkg_path}: {e}")

    return editor


# ---------------------------------------------------------------------------
# Step: makepkg.conf review
# ---------------------------------------------------------------------------

def _step_makepkg(config, state, options, editor: str) -> str:
    _log.ui("─── System makepkg.conf ─────────────────────────────")

    from sysforge.primitives.config import parse_system_makepkg_conf

    conf_path = Path("/etc/makepkg.conf")
    conf = parse_system_makepkg_conf()

    if not conf:
        _log.warn(f"  Could not read {conf_path}")
        return editor

    for key in _MAKEPKG_CONF_HIGHLIGHT:
        if key in conf:
            _log.ui(f"  {key:<12} = {conf[key]}")

    _log.ui(
        "  (sysforge profile overrides CFLAGS / CXXFLAGS / LDFLAGS at build time)"
    )

    if "BUILDDIR" in conf:
        builddir = Path(conf["BUILDDIR"].strip("\"'")).expanduser()
        if not builddir.exists():
            _log.warn(
                f"  ⚠  BUILDDIR {str(builddir)!r} does not exist"
            )
        else:
            try:
                free_gb = shutil.disk_usage(builddir).free / (1024 ** 3)
                _log.ui(f"  BUILDDIR free: {free_gb:.1f} GB")
            except OSError:
                pass

    if _interactive() and not options.dry_run:
        if _prompt("  Edit /etc/makepkg.conf? (requires sudo) [e/↵ skip]: ").lower() == "e":
            result = subprocess.run(["sudo", editor, str(conf_path)])
            if result.returncode != 0:
                _log.warn("  Editor exited non-zero — makepkg.conf may be unchanged")

    return editor


# ---------------------------------------------------------------------------
# Step: user / sudo verification
# ---------------------------------------------------------------------------

def _step_sudo(config, state, options, editor: str) -> str:
    _log.ui("─── User / sudo verification ────────────────────────")

    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "(unknown)"
    _log.ui(f"  Running as: {user}")

    if os.geteuid() == 0:
        _log.warn(
            "  Running as root. SysForge should be run as a regular user with sudo access."
        )
        return editor

    result = subprocess.run(["sudo", "-n", "true"], capture_output=True)
    if result.returncode == 0:
        _log.ui("  sudo: OK (passwordless)")
        return editor

    result2 = subprocess.run(["id", "-Gn", user], capture_output=True, text=True)
    groups = result2.stdout.strip().split() if result2.returncode == 0 else []
    if "wheel" in groups or "sudo" in groups:
        _log.ui("  sudo: requires password (user is in wheel/sudo — OK)")
    else:
        _log.warn(
            f"  sudo: user {user!r} is not in wheel or sudo group. "
            "makepkg -si will fail when trying to install packages."
        )
    return editor


# ---------------------------------------------------------------------------
# Step: disk space
# ---------------------------------------------------------------------------

def _step_disk(config, state, options, editor: str) -> str:
    _log.ui("─── Disk space ──────────────────────────────────────")

    build_dir = Path(
        config.get("paths", {}).get("pkgbuild_dir", "~")
    ).expanduser()

    check_dir = build_dir
    while not check_dir.exists() and check_dir != check_dir.parent:
        check_dir = check_dir.parent

    try:
        usage = shutil.disk_usage(check_dir)
        free_gb  = usage.free  / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
    except OSError as e:
        _log.warn(f"  Could not check disk space on {check_dir}: {e}")
        return editor

    # Count AUR/git packages for estimate
    n_aur = 0
    try:
        pkg_path = resolve_packages_path(config)
        if pkg_path.exists():
            with open(pkg_path, "rb") as f:
                data = tomllib.load(f)
            n_aur = sum(
                1 for p in data.get("package", [])
                if p.get("source", "aur") in ("aur", "git")
            )
    except Exception:
        pass

    est_gb = n_aur * _DISK_PER_PKG_GB
    _log.ui(
        f"  Build dir: {build_dir}  ({free_gb:.1f} GB free of {total_gb:.1f} GB)"
    )
    if n_aur:
        _log.ui(
            f"  Estimated build space: ~{est_gb} GB  "
            f"({n_aur} AUR/git packages × {_DISK_PER_PKG_GB} GB each)"
        )

    if free_gb < _DISK_WARN_GB:
        _log.warn(
            f"  ⚠  Only {free_gb:.1f} GB free — "
            f"recommended minimum is {_DISK_WARN_GB} GB"
        )
    elif n_aur and free_gb < est_gb:
        _log.warn(
            f"  ⚠  Estimated requirement ({est_gb} GB) exceeds "
            f"available space ({free_gb:.1f} GB)"
        )
    else:
        _log.ui("  Disk space: OK")

    return editor


# ---------------------------------------------------------------------------
# Step: network probe
# ---------------------------------------------------------------------------

def _probe_host(host: str, port: int = 443, timeout: int = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _step_network(config, state, options, editor: str) -> str:
    _log.ui("─── Network connectivity ────────────────────────────")

    endpoints = [
        ("AUR",        "aur.archlinux.org", 443),
        ("GitHub",     "github.com",        443),
        ("Arch Linux", "archlinux.org",     443),
    ]

    try:
        for line in Path("/etc/pacman.d/mirrorlist").read_text().splitlines():
            if line.strip().startswith("Server"):
                m = re.search(r"https?://([^/]+)/", line)
                if m:
                    endpoints.append(("Pacman mirror", m.group(1), 443))
                    break
    except OSError:
        pass

    all_ok = True
    for label, host, port in endpoints:
        ok = _probe_host(host, port)
        sym = "✓" if ok else "✗"
        if ok:
            _log.ui(f"  {sym}  {label} ({host})")
        else:
            _log.warn(f"  {sym}  {label} ({host}) — unreachable")
            all_ok = False

    if not all_ok:
        _log.warn(
            "  Some endpoints unreachable. Package clones or downloads may fail."
        )
    return editor


# ---------------------------------------------------------------------------
# Step: GPG keyring bootstrap
# ---------------------------------------------------------------------------

def _step_gpg(config, state, options, editor: str) -> str:
    _log.ui("─── GPG keyring bootstrap ───────────────────────────")

    if not shutil.which("gpg"):
        _log.warn("  gpg not found — key verification unavailable")
        return editor

    r = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True,
    )
    key_count = r.stdout.count("\npub:") if r.returncode == 0 else 0
    _log.ui(f"  GPG keyring: {key_count} public key(s)")

    global_keys_dir = CONFIG_BASE / "etc/sysforge/keys/pgp"
    if global_keys_dir.is_dir():
        asc_files = sorted(global_keys_dir.glob("*.asc"))
        if asc_files:
            _log.ui(
                f"  Importing {len(asc_files)} key(s) from {global_keys_dir}"
            )
            if not options.dry_run:
                r = subprocess.run(
                    ["gpg", "--import", *[str(f) for f in asc_files]],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    _log.warn(f"  GPG import failed:\n{r.stderr.strip()}")
                else:
                    _log.ui("  GPG: global key import succeeded")
        else:
            _log.ui(f"  {global_keys_dir}: no .asc files")
    else:
        _log.ui(
            f"  No global key store at {global_keys_dir} "
            "(per-build keys still imported by the build stage)"
        )

    if _interactive() and not options.dry_run:
        choice = _prompt(
            "  Refresh all keys from keyserver? (gpg --refresh-keys) [y/N]: "
        ).lower()
        if choice == "y":
            _log.ui("Running gpg --refresh-keys (this may take a while)...")
            r = subprocess.run(["gpg", "--refresh-keys"])
            if r.returncode != 0:
                _log.warn("  gpg --refresh-keys failed")
            else:
                _log.ui("GPG: keyring refresh complete")

    return editor


# ---------------------------------------------------------------------------
# Step: build preview
# ---------------------------------------------------------------------------

def _step_preview(config, state, options, editor: str) -> str:
    _log.ui("─── Build preview ───────────────────────────────────")

    pkg_path = resolve_packages_path(config)
    if not pkg_path.exists():
        _log.ui(f"  packages.toml not found at {pkg_path} — skipping")
        return editor

    try:
        with open(pkg_path, "rb") as f:
            pkg_data = tomllib.load(f)
        packages  = pkg_data.get("package", [])
        build_cfg = pkg_data.get("build", {})
    except Exception as e:
        _log.warn(f"  Could not load packages.toml: {e}")
        return editor

    if not packages:
        _log.ui("  No packages defined in packages.toml")
        return editor

    repo_mode = build_cfg.get("repo_mode", "pacman")

    _match_rules = None
    try:
        rules = config.get("rules", [])
        from sysforge.primitives.profile import match_rules as _match_rules
        can_match = True
    except Exception:
        rules, can_match = [], False

    _log.ui(f"  {'Package':<30}  {'Source':<6}  {'Action'}")
    _log.ui(f"  {'─'*30}  {'─'*6}  {'─'*30}")

    repo_count = build_count = 0
    for pkg in packages:
        name   = pkg.get("name", "?")
        source = pkg.get("source", "aur")
        effective_mode = "profiled" if pkg.get("pkgbuild_patch") else repo_mode

        if source == "repo" and effective_mode == "profiled":
            patch_note = " (pkgbuild_patch)" if pkg.get("pkgbuild_patch") else " (repo_mode)"
            action = f"build  [profiled]{patch_note}"
            build_count += 1
        elif source == "repo":
            action = "pacman -S --needed"
            repo_count += 1
        else:
            build_count += 1
            if can_match and _match_rules is not None:
                fake_meta = {"pkgname": name, "pkgbase": name, "makedepends": []}
                matched = _match_rules(fake_meta, rules)
                if matched:
                    winner = max(matched, key=lambda r: r.get("priority", 0))
                    action = f"build  [{winner['profile']}]"
                else:
                    action = f"build  [{config.get('defaults', {}).get('profile', 'standard')}] (default)"
            else:
                action = "build"
        _log.ui(f"  {name:<30}  {source:<6}  {action}")

    _log.ui(
        f"  Total: {len(packages)}  |  repo_mode: {repo_mode}  |  "
        f"Repo (pacman): {repo_count}  |  Builds: {build_count}"
    )
    if can_match:
        _log.ui(
            "  Profiles are tentative (pkgname rules only). "
            "makedepends rules resolve per-PKGBUILD at build time."
        )

    if TOOLCHAIN_PATH.exists():
        try:
            with open(TOOLCHAIN_PATH, "rb") as f:
                tcfg = tomllib.load(f)
            compiler = tcfg.get("compiler", "llvm")
            pgo = tcfg.get("pgo", True) if compiler == "llvm" else False
            pgo_label = " + PGO (3-pass)" if pgo else ""
            _log.ui(f"  Toolchain: {compiler}{pgo_label}")
        except Exception:
            _log.ui("  Toolchain: toolchain.toml present but unreadable")
    else:
        _log.ui("  Toolchain: no toolchain.toml — toolchain stage will be a no-op")

    if KERNEL_PATH.exists():
        try:
            with open(KERNEL_PATH, "rb") as f:
                kcfg = tomllib.load(f)
            _log.ui(
                f"  Kernel: {kcfg.get('pkgname', '?')}  "
                f"(bootloader: {kcfg.get('bootloader', 'systemd-boot')})"
            )
        except Exception:
            _log.ui("  Kernel: kernel.toml present but unreadable")
    else:
        _log.ui("  Kernel: no kernel.toml — kernel stage will be a no-op")

    return editor


# ---------------------------------------------------------------------------
# Step dispatch
# ---------------------------------------------------------------------------

_STEP_FNS = {
    "editor":     _step_editor,
    "config":     _step_config,
    "build_mode": _step_build_mode,
    "makepkg":    _step_makepkg,
    "sudo":       _step_sudo,
    "disk":       _step_disk,
    "network":    _step_network,
    "gpg":        _step_gpg,
    "preview":    _step_preview,
}


def _run_selected_steps(step_keys: list[str], config, state, options) -> None:
    """
    Run selected steps in order. Editor is resolved upfront and threaded
    through — the editor step may update it for subsequent steps.
    """
    editor, _ = _resolve_editor()
    for key in step_keys:
        editor = _STEP_FNS[key](config, state, options, editor)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class ReconfigureStage(Stage):
    name = "reconfigure"
    description = "Pre-build checkpoint — review configs and system state"
    depends_on = ["configure"]
    stateless = True

    def run(self, config, state, options):
        if Path("/run/archiso").exists():
            from sysforge.pipeline.stages._bootstrap import load_bootstrap
            cfg = load_bootstrap()
            chroot_state_dir = Path(cfg.target) / "var/lib/sysforge"
            chroot_state_dir.mkdir(parents=True, exist_ok=True)
            if state.path.exists():
                try:
                    shutil.copy2(state.path, chroot_state_dir / state.path.name)
                    _log.info(f"State file copied to {chroot_state_dir / state.path.name}")
                except shutil.SameFileError:
                    _log.info(f"State file already up-to-date at {chroot_state_dir / state.path.name}")
            raise BootstrapRebootRequired(
                "Bootstrap complete (stages 1–4 done). "
                "Reboot into the installed system before continuing."
            )

        reminder = Path("/etc/profile.d/sysforge-resume.sh")
        if reminder.exists():
            try:
                reminder.unlink()
                _log.ui("Removed login reminder (/etc/profile.d/sysforge-resume.sh)")
            except PermissionError:
                _log.warn(
                    f"Cannot remove {reminder} — run as root or delete it manually"
                )

        _show_stage_summary(state)

        step_keys = _select_steps(options)
        _run_selected_steps(step_keys, config, state, options)

        if _interactive() and not options.dry_run and not options.standalone:
            _log.ui("─────────────────────────────────────────────────────")
            choice = _prompt(
                "[RECONFIGURE] Ready to proceed to toolchain → packages → kernel? [y/N]: "
            ).lower()
            if choice != "y":
                raise RuntimeError(
                    "[RECONFIGURE] Aborted by user. Run with --resume to return to this stage."
                )

        _log.ui("Pre-build checkpoint complete.")
