"""
stages/configure.py — stage 4: configuration checkpoint

Runs after hardware detection and before toolchain/packages/kernel.
Acts as the single "commit point" before any build work begins.

Responsibilities (in order):
  1. Stage progress summary — show prior stage outcomes
  2. Editor selection — resolve SYSFORGE_EDITOR → sysforge.toml → $EDITOR → $VISUAL → vi;
     offer to change temporarily or permanently (saved to /etc/sysforge/sysforge.toml)
  3. Config file review — offer interactive editing of:
       flag_profiles.toml  (with full profile resolution validation on save)
       packages.toml
       kernel.toml         (if present)
       hardware_profile.toml (if present; shown with a safety warning)
  4. System identity — review hostname, locale, timezone, keymap
  5. Pacman configuration — mirrorlist (offer reflector), ParallelDownloads
  6. User / sudo verification — confirm build user and sudoers are correct
  7. Disk space check — estimate required space, warn if headroom is low
  8. Network connectivity probe — verify AUR, GitHub, and mirrors are reachable
  9. Build preview (dry-run) — show what packages + kernel stages will do

Non-interactive mode: when stdin is not a TTY (or options.dry_run is True),
all prompts are skipped. Checks (disk, network, sudo) still run and log.
options.dry_run additionally skips all writes (no edits, no sysforge.toml update).
"""
import os
import shutil
import socket
import subprocess
import sys
import tomllib
from pathlib import Path

import sysforge.log as _log
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.state import resolve_state_dir
from sysforge.primitives.config import (
    CONFIG_BASE,
    PACKAGES_PATH,
    load_config,
    load_conflict_groups,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYSFORGE_TOML_PATH = CONFIG_BASE / "etc/sysforge/sysforge.toml"

# Hard-coded to avoid circular import from stages/__init__.py
_PIPELINE_STAGES = [
    ("partition",    "Disk partitioning"),
    ("base_install", "Base system install"),
    ("hardware",     "Hardware detection"),
    ("configure",    "Configuration checkpoint"),
    ("toolchain",    "LLVM toolchain build"),
    ("packages",     "Build and install packages"),
    ("kernel",       "Build and install custom kernel"),
]

_DISK_WARN_GB   = 20   # warn if free space below this
_DISK_PER_PKG_GB = 3   # rough estimate per AUR/git package


# ---------------------------------------------------------------------------
# Helpers: sysforge.toml (read / write [ui] section)
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
    """Write / update a single [ui] key in sysforge.toml."""
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
    """True when stdin is a real TTY (not piped / scripted)."""
    return sys.stdin.isatty()


def _prompt(msg: str, default: str = "") -> str:
    """Prompt the user and return stripped input. Returns default on EOF."""
    try:
        return input(msg).strip()
    except EOFError:
        return default


# ---------------------------------------------------------------------------
# 1. Stage progress summary
# ---------------------------------------------------------------------------

def _show_stage_summary(state) -> None:
    _log.info("[CONFIGURE]", "─── Pipeline progress ───────────────────────────────")
    _symbols = {
        "done":       "✓",
        "skipped_to": "↷",
        "running":    "→",
        "failed":     "✗",
    }
    for name, desc in _PIPELINE_STAGES:
        status = state.stage_status(name)
        if name == "configure":
            symbol, label = "→", "running"
        else:
            symbol = _symbols.get(status, "·")
            label  = status or "pending"
        _log.info("[CONFIGURE]", f"  {symbol}  {name:<16}  {label}")
    _log.info("[CONFIGURE]", "─────────────────────────────────────────────────────")


# ---------------------------------------------------------------------------
# 2. Editor selection
# ---------------------------------------------------------------------------

def _resolve_editor() -> tuple[str, str]:
    """Return (editor_command, source_description)."""
    sysforge_cfg = _load_sysforge_toml()
    candidates = [
        (os.environ.get("SYSFORGE_EDITOR"), "SYSFORGE_EDITOR"),
        (sysforge_cfg.get("ui", {}).get("editor"), "sysforge.toml"),
        (os.environ.get("EDITOR"), "$EDITOR"),
        (os.environ.get("VISUAL"), "$VISUAL"),
        ("vi", "default"),
    ]
    for value, source in candidates:
        if value:
            return value, source
    return "vi", "default"


def _maybe_change_editor(editor: str, source: str, dry_run: bool) -> str:
    """Show current editor, offer to change it. Returns the editor to use."""
    _log.info("[CONFIGURE]", f"Editor: {editor}  (from {source})")

    if not _interactive() or dry_run:
        return editor

    choice = _prompt("  Change editor? [e]dit / [↵] keep: ")
    if choice.lower() != "e":
        return editor

    new_editor = _prompt(f"  Enter editor command [{editor}]: ") or editor
    if new_editor == editor:
        return editor

    if not dry_run:
        save = _prompt("  Save as sysforge default? [y/N]: ").lower()
        if save == "y":
            try:
                _save_sysforge_toml_ui("editor", new_editor)
                _log.info("[CONFIGURE]", f"  Saved to {SYSFORGE_TOML_PATH}")
            except OSError as e:
                _log.warn("[CONFIGURE]", f"  Could not save preference: {e}")

    return new_editor


# ---------------------------------------------------------------------------
# 3. Config file review
# ---------------------------------------------------------------------------

def _validate_flag_profiles(path: Path) -> tuple[bool, str]:
    """
    Full validation: TOML parse → load_config → merge_extends for all profiles.
    Returns (ok, message).
    """
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except Exception as e:
        return False, f"TOML parse error: {e}"

    try:
        cfg = load_config(config_paths=[path])
        conflict_groups = load_conflict_groups()

        from sysforge.primitives.profile import merge_extends
        profiles = cfg.get("profiles", {})
        for name in profiles:
            merge_extends(name, profiles, conflict_groups=conflict_groups)

        n_profiles = len(profiles)
        n_rules    = len(cfg.get("rules", []))
        return True, f"{n_profiles} profiles, {n_rules} rules"
    except Exception as e:
        return False, str(e)


def _open_in_editor(path: Path, editor: str) -> bool:
    """Spawn editor on path. Returns True if it exited cleanly."""
    try:
        result = subprocess.run([editor, str(path)])
        return result.returncode == 0
    except FileNotFoundError:
        _log.warn("[CONFIGURE]", f"Editor not found: {editor!r}")
        return False


def _review_config_file(
    label:       str,
    path:        Path,
    editor:      str,
    dry_run:     bool,
    validate_fn=None,
    warn:        str | None = None,
) -> None:
    """
    Offer to review/edit a config file.
    If validate_fn is set, run it after each edit and loop on failure.
    """
    exists = path.exists()
    status = str(path) if exists else f"{path}  (not found — skipping)"
    _log.info("[CONFIGURE]", f"  {label}: {status}")

    if not exists or not _interactive() or dry_run:
        return

    choice = _prompt(f"    [e]dit / [↵] skip: ")
    if choice.lower() != "e":
        return

    if warn:
        _log.warn("[CONFIGURE]", f"    ⚠  {warn}")
        confirm = _prompt("    Proceed? [y/N]: ").lower()
        if confirm != "y":
            return

    while True:
        _open_in_editor(path, editor)

        if validate_fn is None:
            break

        _log.info("[CONFIGURE]", f"    Validating {label}...")
        ok, msg = validate_fn(path)
        if ok:
            _log.info("[CONFIGURE]", f"    ✓ {msg}")
            break
        else:
            _log.warn("[CONFIGURE]", f"    ✗ {msg}")
            action = _prompt("    [r]e-open in editor / [s]kip (keep previous) / [a]bort: ").lower()
            if action == "r":
                continue
            elif action == "a":
                raise RuntimeError(f"[CONFIGURE] Aborted at {label} validation failure")
            else:
                _log.warn("[CONFIGURE]", "    Skipping — file may be invalid, proceeding with caution")
                break


def _review_all_configs(config: dict, state, editor: str, dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── Config file review ──────────────────────────────")

    # flag_profiles.toml — primary config, full validation
    flag_profiles_path = CONFIG_BASE / "etc/sysforge/flag_profiles.toml"
    _review_config_file(
        "flag_profiles.toml", flag_profiles_path, editor, dry_run,
        validate_fn=_validate_flag_profiles,
    )

    # packages.toml — package manifest, TOML parse only
    _review_config_file(
        "packages.toml", PACKAGES_PATH, editor, dry_run,
    )

    # kernel.toml — only if present
    kernel_path = CONFIG_BASE / "etc/sysforge/kernel.toml"
    if kernel_path.exists():
        _review_config_file(
            "kernel.toml", kernel_path, editor, dry_run,
        )

    # hardware_profile.toml — generated; editable with warning
    state_dir, _ = resolve_state_dir(None)
    hw_path = Path(config.get("hardware_profile") or state_dir / "hardware_profile.toml")
    if hw_path.exists():
        _review_config_file(
            "hardware_profile.toml", hw_path, editor, dry_run,
            warn="This file is machine-generated by the hardware stage. "
                 "Manual edits can cause driver mismatches or broken kconfig.",
        )


# ---------------------------------------------------------------------------
# 4. System identity
# ---------------------------------------------------------------------------

def _read_file_stripped(path: str) -> str:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return "(not set)"


def _read_timezone() -> str:
    try:
        tz_path = Path("/etc/localtime")
        if tz_path.is_symlink():
            target = str(tz_path.resolve())
            # Strip the /usr/share/zoneinfo/ prefix
            marker = "zoneinfo/"
            idx = target.find(marker)
            return target[idx + len(marker):] if idx != -1 else target
        return "(not set)"
    except OSError:
        return "(not set)"


def _check_system_identity(dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── System identity ─────────────────────────────────")

    hostname = _read_file_stripped("/etc/hostname")
    locale   = _read_file_stripped("/etc/locale.conf")
    timezone = _read_timezone()
    keymap   = _read_file_stripped("/etc/vconsole.conf")

    _log.info("[CONFIGURE]", f"  hostname:  {hostname}")
    _log.info("[CONFIGURE]", f"  locale:    {locale}")
    _log.info("[CONFIGURE]", f"  timezone:  {timezone}")
    _log.info("[CONFIGURE]", f"  keymap:    {keymap}")

    if not _interactive() or dry_run:
        return

    choice = _prompt("  Edit system identity? [y/N]: ").lower()
    if choice != "y":
        return

    _log.info("[CONFIGURE]",
        "  Edit via:\n"
        "    hostname:  hostnamectl set-hostname <name>  (or edit /etc/hostname)\n"
        "    locale:    localectl set-locale LANG=<value>  (or edit /etc/locale.conf)\n"
        "    timezone:  timedatectl set-timezone <region/city>\n"
        "    keymap:    localectl set-keymap <keymap>  (or edit /etc/vconsole.conf)\n"
        "  Open a shell, make changes, then return here."
    )
    _prompt("  Press [↵] when done: ")


# ---------------------------------------------------------------------------
# 5. Pacman configuration
# ---------------------------------------------------------------------------

def _check_pacman_config(dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── Pacman configuration ────────────────────────────")

    # ParallelDownloads from /etc/pacman.conf
    parallel = "(not set)"
    try:
        for line in Path("/etc/pacman.conf").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("ParallelDownloads"):
                parallel = stripped
                break
    except OSError:
        parallel = "(could not read /etc/pacman.conf)"
    _log.info("[CONFIGURE]", f"  ParallelDownloads: {parallel}")

    # Mirrorlist
    mirrorlist = Path("/etc/pacman.d/mirrorlist")
    if mirrorlist.exists():
        lines = [l for l in mirrorlist.read_text().splitlines()
                 if l.strip().startswith("Server")]
        _log.info("[CONFIGURE]", f"  Mirrorlist: {len(lines)} active server(s) in {mirrorlist}")
    else:
        _log.warn("[CONFIGURE]", f"  Mirrorlist not found at {mirrorlist}")

    if not _interactive() or dry_run:
        return

    choice = _prompt("  Run reflector to update mirrorlist? [y/N]: ").lower()
    if choice != "y":
        return

    # Check reflector is available
    if not shutil.which("reflector"):
        _log.warn("[CONFIGURE]", "  reflector is not installed. Install with: sudo pacman -S reflector")
        return

    countries = _prompt("  Country codes for reflector (e.g. 'US,GB') [↵ for none]: ")
    cmd = ["sudo", "reflector", "--protocol", "https", "--sort", "rate",
           "--save", "/etc/pacman.d/mirrorlist"]
    if countries:
        for country in countries.split(","):
            cmd += ["--country", country.strip()]

    _log.info("[CONFIGURE]", f"  Running: {' '.join(cmd)}")
    result = subprocess.run(cmd)
    if result.returncode != 0:
        _log.warn("[CONFIGURE]", "  reflector exited with errors — mirrorlist may be unchanged")
    else:
        _log.info("[CONFIGURE]", "  Mirrorlist updated.")


# ---------------------------------------------------------------------------
# 6. User / sudo verification
# ---------------------------------------------------------------------------

def _check_user_sudo() -> None:
    _log.info("[CONFIGURE]", "─── User / sudo verification ────────────────────────")

    user = os.environ.get("SUDO_USER") or os.environ.get("USER") or "(unknown)"
    _log.info("[CONFIGURE]", f"  Running as: {user}")

    if os.geteuid() == 0:
        _log.warn("[CONFIGURE]",
            "  Running as root. SysForge should be run as a regular user with sudo access."
        )
        return

    # Check sudo access
    result = subprocess.run(
        ["sudo", "-n", "true"],
        capture_output=True,
    )
    if result.returncode == 0:
        _log.info("[CONFIGURE]", "  sudo: OK (passwordless)")
        return

    # Needs password — check if user is in wheel/sudo group
    result2 = subprocess.run(["id", "-Gn", user], capture_output=True, text=True)
    groups = result2.stdout.strip().split() if result2.returncode == 0 else []

    if "wheel" in groups or "sudo" in groups:
        _log.info("[CONFIGURE]", "  sudo: requires password (user is in wheel/sudo group — OK)")
    else:
        _log.warn("[CONFIGURE]",
            f"  sudo: user {user!r} is not in the wheel or sudo group. "
            "makepkg -si will fail when trying to install packages."
        )


# ---------------------------------------------------------------------------
# 7. Disk space check
# ---------------------------------------------------------------------------

def _count_aur_packages(config: dict) -> int:
    """Return number of aur/git packages in packages.toml. Returns 0 on any error."""
    path = config.get("packages_file")
    path = Path(path) if path else PACKAGES_PATH
    if not path.exists():
        return 0
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return sum(
            1 for p in data.get("package", [])
            if p.get("source", "aur") in ("aur", "git")
        )
    except Exception:
        return 0


def _check_disk_space(config: dict) -> None:
    _log.info("[CONFIGURE]", "─── Disk space ──────────────────────────────────────")

    build_dir_raw = config.get("paths", {}).get("pkgbuild_dir", "~")
    build_dir = Path(build_dir_raw).expanduser()

    # Walk up to find the mountpoint that actually exists
    check_dir = build_dir
    while not check_dir.exists() and check_dir != check_dir.parent:
        check_dir = check_dir.parent

    try:
        usage = shutil.disk_usage(check_dir)
        free_gb  = usage.free  / (1024 ** 3)
        total_gb = usage.total / (1024 ** 3)
    except OSError as e:
        _log.warn("[CONFIGURE]", f"  Could not check disk space on {check_dir}: {e}")
        return

    n_aur = _count_aur_packages(config)
    est_gb = n_aur * _DISK_PER_PKG_GB

    _log.info("[CONFIGURE]",
        f"  Build dir: {build_dir}  ({free_gb:.1f} GB free of {total_gb:.1f} GB)"
    )
    if n_aur:
        _log.info("[CONFIGURE]",
            f"  Estimated build space: ~{est_gb} GB  ({n_aur} AUR/git packages × {_DISK_PER_PKG_GB} GB each)"
        )

    if free_gb < _DISK_WARN_GB:
        _log.warn("[CONFIGURE]",
            f"  ⚠  Only {free_gb:.1f} GB free — recommended minimum is {_DISK_WARN_GB} GB. "
            "Large builds (LLVM, kernels) may fail mid-way."
        )
    elif n_aur and free_gb < est_gb:
        _log.warn("[CONFIGURE]",
            f"  ⚠  Estimated requirement ({est_gb} GB) exceeds available space ({free_gb:.1f} GB)."
        )
    else:
        _log.info("[CONFIGURE]", "  Disk space: OK")


# ---------------------------------------------------------------------------
# 8. Network connectivity probe
# ---------------------------------------------------------------------------

def _probe_host(host: str, port: int = 443, timeout: int = 5) -> bool:
    try:
        socket.create_connection((host, port), timeout=timeout).close()
        return True
    except OSError:
        return False


def _probe_network(config: dict) -> None:
    _log.info("[CONFIGURE]", "─── Network connectivity ────────────────────────────")

    endpoints = [
        ("AUR",        "aur.archlinux.org",     443),
        ("GitHub",     "github.com",             443),
        ("Arch Linux", "archlinux.org",          443),
    ]

    # Add first mirror hostname from mirrorlist if present
    try:
        for line in Path("/etc/pacman.d/mirrorlist").read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith("Server"):
                # Extract hostname from URL
                import re
                m = re.search(r"https?://([^/]+)/", stripped)
                if m:
                    endpoints.append(("Pacman mirror", m.group(1), 443))
                    break
    except OSError:
        pass

    all_ok = True
    for label, host, port in endpoints:
        ok = _probe_host(host, port)
        if ok:
            _log.info("[CONFIGURE]", f"  ✓  {label} ({host})")
        else:
            _log.warn("[CONFIGURE]", f"  ✗  {label} ({host}) — unreachable")
            all_ok = False

    if not all_ok:
        _log.warn("[CONFIGURE]",
            "  Some endpoints unreachable. Package clones or downloads may fail."
        )


# ---------------------------------------------------------------------------
# 4b. System makepkg.conf review
# ---------------------------------------------------------------------------

# Keys from /etc/makepkg.conf most relevant to show before a build run.
_MAKEPKG_CONF_HIGHLIGHT = [
    "MAKEFLAGS", "BUILDDIR", "PKGDEST", "PKGEXT",
    "CFLAGS", "CXXFLAGS", "LDFLAGS",
]


def _review_makepkg_conf(editor: str, dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── System makepkg.conf ─────────────────────────────")

    from sysforge.primitives.config import parse_system_makepkg_conf

    conf_path = Path("/etc/makepkg.conf")
    conf = parse_system_makepkg_conf()

    if not conf:
        _log.warn("[CONFIGURE]", f"  Could not read {conf_path}")
        return

    for key in _MAKEPKG_CONF_HIGHLIGHT:
        if key in conf:
            _log.info("[CONFIGURE]", f"  {key:<12} = {conf[key]}")

    _log.info("[CONFIGURE]",
        "  (sysforge profile overrides CFLAGS / CXXFLAGS / LDFLAGS at build time)"
    )

    # Warn if BUILDDIR doesn't exist or its mount is low on space
    if "BUILDDIR" in conf:
        builddir = Path(conf["BUILDDIR"].strip("\"'")).expanduser()
        if not builddir.exists():
            _log.warn("[CONFIGURE]",
                f"  ⚠  BUILDDIR {str(builddir)!r} does not exist — "
                "makepkg will create it, or fail if the parent mount is missing"
            )
        else:
            try:
                free_gb = shutil.disk_usage(builddir).free / (1024 ** 3)
                _log.info("[CONFIGURE]", f"  BUILDDIR free: {free_gb:.1f} GB")
            except OSError:
                pass

    if not _interactive() or dry_run:
        return

    choice = _prompt("  Edit /etc/makepkg.conf? (requires sudo) [e/↵ skip]: ")
    if choice.lower() != "e":
        return

    result = subprocess.run(["sudo", editor, str(conf_path)])
    if result.returncode != 0:
        _log.warn("[CONFIGURE]", "  Editor exited non-zero — makepkg.conf may be unchanged")


# ---------------------------------------------------------------------------
# 8b. GPG keyring bootstrap
# ---------------------------------------------------------------------------

def _bootstrap_gpg(dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── GPG keyring bootstrap ───────────────────────────")

    if not shutil.which("gpg"):
        _log.warn("[CONFIGURE]", "  gpg not found — key verification will be unavailable during builds")
        return

    # Report current keyring size
    r = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        capture_output=True, text=True,
    )
    key_count = r.stdout.count("\npub:") if r.returncode == 0 else 0
    _log.info("[CONFIGURE]", f"  GPG keyring: {key_count} public key(s)")

    # Import from the sysforge-managed global key store (shared across packages)
    global_keys_dir = CONFIG_BASE / "etc/sysforge/keys/pgp"
    if global_keys_dir.is_dir():
        asc_files = sorted(global_keys_dir.glob("*.asc"))
        if asc_files:
            _log.info("[CONFIGURE]",
                f"  Importing {len(asc_files)} key(s) from {global_keys_dir}"
            )
            if not dry_run:
                r = subprocess.run(
                    ["gpg", "--import", *[str(f) for f in asc_files]],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    _log.warn("[CONFIGURE]", f"  GPG import failed:\n{r.stderr.strip()}")
                else:
                    _log.info("[CONFIGURE]", "  GPG: global key import succeeded")
        else:
            _log.info("[CONFIGURE]", f"  {global_keys_dir}: no .asc files")
    else:
        _log.info("[CONFIGURE]",
            f"  No global key store at {global_keys_dir} "
            "(per-build keys are still imported by the build stage)"
        )

    if not _interactive() or dry_run:
        return

    # Offer keyserver refresh — useful before a long unattended build run
    choice = _prompt(
        "  Refresh all keys from keyserver? (gpg --refresh-keys) [y/N]: "
    ).lower()
    if choice != "y":
        return

    _log.info("[CONFIGURE]", "  Running gpg --refresh-keys...")
    r = subprocess.run(["gpg", "--refresh-keys"], capture_output=True, text=True)
    if r.returncode != 0:
        _log.warn("[CONFIGURE]", f"  gpg --refresh-keys failed:\n{r.stderr.strip()}")
    else:
        _log.info("[CONFIGURE]", "  GPG: keyring refresh complete")


# ---------------------------------------------------------------------------
# 9. Build preview
# ---------------------------------------------------------------------------

def _show_build_preview(config: dict, dry_run: bool) -> None:
    _log.info("[CONFIGURE]", "─── Build preview ───────────────────────────────────")

    packages_path = config.get("packages_file")
    packages_path = Path(packages_path) if packages_path else PACKAGES_PATH
    if not packages_path.exists():
        _log.info("[CONFIGURE]", f"  packages.toml not found at {packages_path} — skipping preview")
        return

    try:
        with open(packages_path, "rb") as f:
            data = tomllib.load(f)
        packages = data.get("package", [])
    except Exception as e:
        _log.warn("[CONFIGURE]", f"  Could not load packages.toml: {e}")
        return

    if not packages:
        _log.info("[CONFIGURE]", "  No packages defined in packages.toml")
        return

    # Try to match pkgname-only rules for a tentative profile
    try:
        rules = config.get("rules", [])
        from sysforge.primitives.profile import match_rules
        can_match = True
    except Exception:
        can_match = False

    _log.info("[CONFIGURE]",
        f"  {'Package':<30}  {'Source':<6}  {'Action'}"
    )
    _log.info("[CONFIGURE]", f"  {'─'*30}  {'─'*6}  {'─'*30}")

    repo_count = 0
    build_count = 0
    for pkg in packages:
        name   = pkg.get("name", "?")
        source = pkg.get("source", "aur")

        if source == "repo":
            action = "pacman -S --needed"
            repo_count += 1
        else:
            build_count += 1
            if can_match:
                # Partial match: pkgname only (makedepends rules need PKGBUILD)
                fake_meta = {"pkgname": name, "pkgbase": name, "makedepends": []}
                matched = match_rules(fake_meta, rules)
                if matched:
                    winner = max(matched, key=lambda r: r.get("priority", 0))
                    action = f"build  [{winner['profile']}]"
                else:
                    defaults = config.get("defaults", {})
                    action = f"build  [{defaults.get('profile', 'standard')}] (default)"
            else:
                action = "build"

        _log.info("[CONFIGURE]", f"  {name:<30}  {source:<6}  {action}")

    _log.info("[CONFIGURE]",
        f"  Total: {len(packages)}  |  Repo installs: {repo_count}  |  Builds: {build_count}"
    )
    if can_match:
        _log.info("[CONFIGURE]",
            "  Note: profile shown is tentative (pkgname rules only). "
            "makedepends rules are resolved per-PKGBUILD at build time."
        )

    # Kernel stage preview
    kernel_path = CONFIG_BASE / "etc/sysforge/kernel.toml"
    if kernel_path.exists():
        try:
            with open(kernel_path, "rb") as f:
                kernel_cfg = tomllib.load(f)
            pkgname = kernel_cfg.get("pkgname", "?")
            bootloader = kernel_cfg.get("bootloader", "systemd-boot")
            _log.info("[CONFIGURE]",
                f"  Kernel: {pkgname}  (bootloader: {bootloader})"
            )
        except Exception:
            _log.info("[CONFIGURE]", "  Kernel: kernel.toml present but could not be read")
    else:
        _log.info("[CONFIGURE]",
            "  Kernel: no kernel.toml — kernel stage will be a no-op"
        )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class ConfigureStage(Stage):
    name = "configure"
    description = "Configuration checkpoint — review configs before building"
    depends_on = ["hardware"]

    def run(self, config, state, options):
        # 1. Stage summary
        _show_stage_summary(state)

        # 2. Editor selection
        editor, source = _resolve_editor()
        editor = _maybe_change_editor(editor, source, options.dry_run)

        # 3. Config file review (interactive only; skipped in dry_run)
        if not options.dry_run:
            _review_all_configs(config, state, editor, options.dry_run)
        else:
            _log.info("[CONFIGURE]", "[dry-run] Config file review skipped")

        # 4b. System makepkg.conf review
        _review_makepkg_conf(editor, options.dry_run)

        # 4. System identity
        _check_system_identity(options.dry_run)

        # 5. Pacman configuration
        _check_pacman_config(options.dry_run)

        # 6. User / sudo
        _check_user_sudo()

        # 7. Disk space
        _check_disk_space(config)

        # 8. Network probe
        _probe_network(config)

        # 8b. GPG keyring bootstrap (after network — key fetching needs connectivity)
        _bootstrap_gpg(options.dry_run)

        # 9. Build preview
        _show_build_preview(config, options.dry_run)

        # Final confirmation before proceeding to build stages
        if _interactive() and not options.dry_run:
            _log.info("[CONFIGURE]", "─────────────────────────────────────────────────────")
            choice = _prompt(
                "[CONFIGURE] Ready to proceed to toolchain → packages → kernel? [y/N]: "
            ).lower()
            if choice != "y":
                raise RuntimeError(
                    "[CONFIGURE] Aborted by user at configuration checkpoint. "
                    "Run with --resume to return to this stage."
                )

        _log.info("[CONFIGURE]", "Configuration checkpoint complete.")
