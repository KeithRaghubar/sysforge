# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/configure.py — stage 4: bootstrap configuration

Applies one-time system identity and mirror configuration to a freshly
installed system. All operations run inside arch-chroot so the target
filesystem is modified correctly (symlinks, locale-gen, etc.).

Reads /etc/sysforge/bootstrap.toml for configuration. Stage fails with a
clear error if bootstrap.toml is absent or missing required fields.

bootstrap.toml required fields:
  target              string  mount point of the installed system (e.g. "/mnt")
  [system] hostname   string  hostname to set
  [system] locale     string  locale string (e.g. "en_US.UTF-8")
  [system] timezone   string  timezone (e.g. "UTC", "America/New_York")

bootstrap.toml optional fields:
  [system] keymap              string  vconsole keymap (default: "us")
  [system] parallel_downloads  int     pacman ParallelDownloads (default: 5)
  [system] shell               string  default login shell: "bash" (default) or "zsh"
  [system] root_password       string  root password set via chpasswd (prompted if absent)
  [mirror] countries           list    reflector --country values
  [mirror] protocol            string  reflector --protocol (default: "https")
  [mirror] age                 int     reflector --latest N (default: 12)
  [desktop] environment        string  desktop package group to install (gnome | kde);
                                       interactive prompt when unset on a TTY

These steps are intentionally separated from reconfigure (stage 5) because
they are destructive on a live running system if re-applied carelessly.
Use --start-from reconfigure to skip bootstrap stages on an already-configured
system.
"""

import json
import shutil
import subprocess
import re
from importlib.metadata import distribution, PackageNotFoundError
from pathlib import Path

from sysforge import log
_log = log.get_logger("CONFIGURE")
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import load_bootstrap, BootstrapConfig
from sysforge.primitives.pkg_catalog import select_desktop, write_desktop_group
from sysforge.primitives.prompt import is_interactive
from sysforge.primitives.run import run_or_raise


# ---------------------------------------------------------------------------
# arch-chroot helper
# ---------------------------------------------------------------------------

def _chroot(target: str, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside arch-chroot <target>."""
    full_cmd = ["arch-chroot", target] + cmd
    _log.info(f"chroot: {' '.join(cmd)}")
    return subprocess.run(full_cmd, check=check)


# ---------------------------------------------------------------------------
# Configuration steps
# ---------------------------------------------------------------------------

def _set_hostname(cfg: BootstrapConfig) -> None:
    hostname_file = Path(cfg.target) / "etc/hostname"
    hostname_file.write_text(cfg.hostname + "\n")
    _log.ui(f"Hostname: {cfg.hostname}")


def _set_locale(cfg: BootstrapConfig) -> None:
    # Uncomment the locale in /etc/locale.gen inside the chroot
    locale_gen = Path(cfg.target) / "etc/locale.gen"
    if locale_gen.exists():
        text = locale_gen.read_text()
        # Uncomment lines starting with #<locale> (with optional space)
        pattern = re.compile(
            r"^#\s*(" + re.escape(cfg.locale) + r".*)", re.MULTILINE
        )
        new_text = pattern.sub(r"\1", text)
        if new_text != text:
            locale_gen.write_text(new_text)
            _log.info(f"Uncommented {cfg.locale} in /etc/locale.gen")
        else:
            _log.warn(f"{cfg.locale} not found in /etc/locale.gen — locale-gen may fail")
    else:
        _log.warn(f"{cfg.target}/etc/locale.gen not found — skipping locale.gen edit")

    # Write locale.conf
    locale_conf = Path(cfg.target) / "etc/locale.conf"
    locale_conf.write_text(f"LANG={cfg.locale}\n")
    _log.ui(f"Locale: {cfg.locale}")

    # Run locale-gen inside chroot
    _chroot(cfg.target, ["locale-gen"])


def _set_timezone(cfg: BootstrapConfig) -> None:
    # Create /etc/localtime symlink via timedatectl or direct ln -sf
    tz_path = f"/usr/share/zoneinfo/{cfg.timezone}"
    _chroot(cfg.target, ["ln", "-sf", tz_path, "/etc/localtime"])
    _chroot(cfg.target, ["hwclock", "--systohc"])
    _log.ui(f"Timezone: {cfg.timezone}")


def _set_keymap(cfg: BootstrapConfig) -> None:
    if not cfg.keymap or cfg.keymap == "us":
        # us is the kernel default; vconsole.conf is only needed for non-default
        _log.info("Keymap: us (default, skipping vconsole.conf)")
        return
    vconsole = Path(cfg.target) / "etc/vconsole.conf"
    vconsole.write_text(f"KEYMAP={cfg.keymap}\n")
    _log.ui(f"Keymap: {cfg.keymap}")


def _set_pacman_parallel_downloads(cfg: BootstrapConfig) -> None:
    n = cfg.parallel_downloads
    pacman_conf = Path(cfg.target) / "etc/pacman.conf"
    if not pacman_conf.exists():
        _log.warn(f"{pacman_conf} not found — skipping ParallelDownloads")
        return

    text = pacman_conf.read_text()
    # Replace existing ParallelDownloads = N (commented or not)
    new_text, count = re.subn(
        r"^#?\s*ParallelDownloads\s*=.*$",
        f"ParallelDownloads = {n}",
        text,
        flags=re.MULTILINE,
    )
    if not count:
        # Insert after [options] header
        new_text = re.sub(
            r"(\[options\][^\n]*\n)",
            f"\\1ParallelDownloads = {n}\n",
            text,
            count=1,
        )

    if new_text != text:
        pacman_conf.write_text(new_text)
        _log.ui(f"ParallelDownloads: {n}")


def _run_reflector(cfg: BootstrapConfig) -> None:
    """Run reflector inside the chroot to generate an optimised mirrorlist."""
    # Check reflector is available in the chroot by testing the filesystem
    # (avoid using 'which' — it is not part of the Arch base install)
    if not (Path(cfg.target) / "usr/bin/reflector").exists():
        _log.warn(
            "reflector not found in chroot — skipping mirrorlist update. "
            "Install reflector and re-run, or update /etc/pacman.d/mirrorlist manually."
        )
        return

    cmd = ["reflector", "--protocol", cfg.mirror_protocol,
           "--latest", str(cfg.mirror_age),
           "--sort", "rate",
           "--save", "/etc/pacman.d/mirrorlist"]

    if cfg.mirror_countries:
        for country in cfg.mirror_countries:
            cmd += ["--country", country]

    _log.ui(f"Running reflector: {' '.join(cmd[1:])}")
    result = _chroot(cfg.target, cmd, check=False)
    if result.returncode != 0:
        _log.warn(f"reflector exited {result.returncode} — mirrorlist may be unchanged")
    else:
        _log.ui("Mirrorlist updated.")


def _install_bootloader(cfg: BootstrapConfig) -> None:
    """Install systemd-boot and write a minimal loader entry."""
    _chroot(cfg.target, ["bootctl", "install"])

    loader_conf = Path(cfg.target) / "boot/loader/loader.conf"
    loader_conf.parent.mkdir(parents=True, exist_ok=True)
    loader_conf.write_text("default arch.conf\ntimeout 3\nconsole-mode max\n")

    entries_dir = Path(cfg.target) / "boot/loader/entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    (entries_dir / "arch.conf").write_text(
        "title   Arch Linux\n"
        "linux   /vmlinuz-linux\n"
        "initrd  /initramfs-linux.img\n"
        "options root=LABEL=root rw\n"
    )
    _log.ui("Bootloader: systemd-boot installed")


def _enable_services(cfg: BootstrapConfig) -> None:
    """Enable NetworkManager and sshd so they start on first boot."""
    _chroot(cfg.target, ["systemctl", "enable", "NetworkManager"])
    _chroot(cfg.target, ["systemctl", "enable", "sshd"])
    _log.ui("Services enabled: NetworkManager, sshd")


def _configure_sshd(cfg: BootstrapConfig) -> None:
    """Allow root login via SSH (required for initial access)."""
    sshd_config = Path(cfg.target) / "etc/ssh/sshd_config"
    if not sshd_config.exists():
        _log.warn("sshd_config not found — skipping PermitRootLogin config")
        return
    text = sshd_config.read_text()
    new_text, count = re.subn(
        r"^#?\s*PermitRootLogin\s+.*$",
        "PermitRootLogin yes",
        text,
        flags=re.MULTILINE,
    )
    if not count:
        new_text = text + "\nPermitRootLogin yes\n"
    if new_text != text:
        sshd_config.write_text(new_text)
    _log.ui("sshd: PermitRootLogin yes")


def _create_user(cfg: BootstrapConfig) -> None:
    """Create the primary user, add to wheel, and configure sudo."""
    # Create user with home dir and wheel group membership
    result = _chroot(cfg.target, ["useradd", "-m", "-G", "wheel", cfg.username], check=False)
    if result.returncode not in (0, 9):  # 9 = already exists
        raise RuntimeError(f"[CONFIGURE] useradd failed for {cfg.username!r} (exit {result.returncode})")
    _log.ui(f"User: {cfg.username} (wheel)")

    # Allow wheel group to use sudo via a sudoers drop-in
    sudoers_d = Path(cfg.target) / "etc/sudoers.d"
    sudoers_d.mkdir(parents=True, exist_ok=True)
    (sudoers_d / "wheel").write_text("%wheel ALL=(ALL:ALL) ALL\n")
    _log.ui("sudo: wheel group enabled")

    if cfg.user_password:
        run_or_raise(
            ["arch-chroot", cfg.target, "chpasswd"],
            tag="CONFIGURE", operation="chpasswd",
            hint=f"failed for {cfg.username!r}",
            input=f"{cfg.username}:{cfg.user_password}\n",
        )
        _log.ui(f"Password set for {cfg.username}.")
    else:
        _log.warn(
            f"No user_password in bootstrap.toml — set it manually after reboot:\n"
            f"  passwd {cfg.username}"
        )


_BASHRC = (
    "[[ $- != *i* ]] && return\n"
    "\n"
    "alias ls='ls --color=auto'\n"
    "alias grep='grep --color=auto'\n"
    "\n"
)
_ZSHRC = (
    "autoload -Uz compinit && compinit\n"
    "\n"
    "alias ls='ls --color=auto'\n"
    "alias grep='grep --color=auto'\n"
    "\n"
    "setopt autocd\n"
    "\n"
)


_RESUME_REMINDER = """\
# Written by sysforge bootstrap. Removed automatically when the pipeline resumes.
[ -t 1 ] && printf '\\n  SysForge bootstrap complete. Resume the pipeline:\\n    sysforge run pipeline --resume\\n\\n'
"""

_RESUME_REMINDER_PATH = Path("etc/profile.d/sysforge-resume.sh")

# Path where iso-install.sh stashes the AUR-cloned sysforge source so the
# configure stage can copy it into the target. Wheel-only pacman installs
# don't preserve the source tree, so this cache is the primary handoff.
_ISO_INSTALL_SOURCE_CACHE = Path("/var/cache/sysforge/source")

# Upstream repo URL — used as a last-resort clone target inside the chroot
# when no local source tree can be found. Kept aligned with the AUR PKGBUILD.
_SYSFORGE_REPO_URL = "https://github.com/KeithRaghubar/sysforge.git"


def _find_sysforge_source() -> Path | None:
    """
    Return the sysforge source directory on the live ISO.

    Tries iso-install.sh's source cache first, then pip's direct_url.json
    (editable/path installs), then walks up from sysforge/__file__.
    Returns None if nothing usable is found — the caller should fall back
    to cloning from upstream inside the chroot.
    """
    # Strategy 0: iso-install.sh stash (covers the AUR install path)
    if (_ISO_INSTALL_SOURCE_CACHE / "pyproject.toml").is_file():
        return _ISO_INSTALL_SOURCE_CACHE

    # Strategy 1: pip metadata (direct_url.json from path/editable installs)
    try:
        dist = distribution("sysforge")
        raw = dist.read_text("direct_url.json")
        if raw:
            url = json.loads(raw).get("url", "")
            if url.startswith("file://"):
                p = Path(url[7:])
                if p.is_dir():
                    return p
    except PackageNotFoundError:
        pass

    # Strategy 2: walk up from sysforge/__init__.py to find the repo root
    try:
        import sysforge as _pkg
        pkg_dir = Path(_pkg.__file__).resolve().parent
        # Look for pyproject.toml to identify the repo root
        for candidate in (pkg_dir.parent, pkg_dir.parent.parent):
            if (candidate / "pyproject.toml").exists():
                return candidate
    except Exception:
        pass

    return None


_PKGVER_RE = re.compile(r"^pkgver\s*=\s*['\"]?([^'\"\s]+)['\"]?\s*$", re.MULTILINE)


def _read_pkgver(pkgbuild: Path) -> str:
    """Extract pkgver from a PKGBUILD. Raise RuntimeError if not found."""
    text = pkgbuild.read_text(encoding="utf-8")
    match = _PKGVER_RE.search(text)
    if not match:
        raise RuntimeError(f"[CONFIGURE] could not parse pkgver from {pkgbuild}")
    return match.group(1)


def _install_sysforge(cfg: BootstrapConfig) -> None:
    """Place sysforge source under <target>/root/sysforge and install it.

    Builds the source via makepkg in the chroot and installs the resulting
    package with `pacman -U --overwrite='/etc/sysforge/*'` so the install is
    pacman-tracked (queryable via `pacman -Q sysforge`, removable via
    `pacman -R sysforge`, and upgradable via the normal AUR flow). Build and
    install are split (makepkg -s, then explicit pacman -U) because makepkg
    -si gives no way to forward `--overwrite` to its internal pacman call,
    and the overwrite glob is needed when /etc/sysforge/* already exists
    from a prior `uv pip install`-based bootstrap (those files are unowned
    by pacman and would otherwise abort the install with file-conflict
    errors on a reinstall).

    Prefers a local source tree (iso-install cache, pip metadata, or repo
    walk-up). Falls back to cloning from upstream inside the chroot so the
    pipeline still completes when sysforge was installed by some other means.
    """
    target_src = Path(cfg.target) / "root/sysforge"
    src = _find_sysforge_source()

    if src is not None:
        if target_src.exists():
            shutil.rmtree(target_src)
        shutil.copytree(src, target_src)
        _log.ui(f"sysforge source copied to target ({src} → /root/sysforge)")
    else:
        if target_src.exists():
            shutil.rmtree(target_src)
        _log.warn(
            "No local sysforge source tree found — cloning from "
            f"{_SYSFORGE_REPO_URL} inside the chroot."
        )
        _chroot(cfg.target, ["git", "clone", "--depth", "1", _SYSFORGE_REPO_URL, "/root/sysforge"])
        _log.ui("sysforge source cloned into target (/root/sysforge)")

    pkgbuild = target_src / "PKGBUILD"
    if not pkgbuild.is_file():
        raise RuntimeError(
            f"[CONFIGURE] no PKGBUILD found in sysforge source at {pkgbuild}. "
            "Cannot build a pacman-tracked package."
        )
    pkgver = _read_pkgver(pkgbuild)

    # Build dir owned by the unprivileged user (makepkg refuses to run as root).
    rel_build = Path("home") / cfg.username / "sysforge-pkg"
    build_host = Path(cfg.target) / rel_build
    build_chroot = "/" + str(rel_build)
    if build_host.exists():
        shutil.rmtree(build_host)
    build_host.mkdir(parents=True)

    # Stage source as the tarball the upstream PKGBUILD's source=() expects
    # ("sysforge-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz"). When that
    # filename already exists in SRCDEST, makepkg uses it instead of fetching
    # — so we never need network or the right git tag inside the chroot.
    extract_root = build_host / f"sysforge-{pkgver}"
    shutil.copytree(target_src, extract_root)
    tarball = build_host / f"sysforge-{pkgver}.tar.gz"
    subprocess.run(
        ["tar", "-C", str(build_host), "-czf", str(tarball), f"sysforge-{pkgver}"],
        check=True,
    )
    shutil.rmtree(extract_root)
    shutil.copy(pkgbuild, build_host / "PKGBUILD")

    _chroot(cfg.target, ["chown", "-R", f"{cfg.username}:{cfg.username}", build_chroot])

    # makepkg -s calls `sudo pacman -S` to sync makedeps, which needs to run
    # non-interactively. Grant the build user passwordless sudo for the
    # duration of the build. Removed in the finally block below.
    sudoers_drop = Path(cfg.target) / "etc/sudoers.d/99-sysforge-bootstrap-build"
    sudoers_drop.parent.mkdir(parents=True, exist_ok=True)
    sudoers_drop.write_text(f"{cfg.username} ALL=(ALL) NOPASSWD: ALL\n")
    sudoers_drop.chmod(0o440)

    try:
        # Step 1: build only (no -i) so we can pass --overwrite to pacman.
        result = _chroot(
            cfg.target,
            [
                "sudo", "-u", cfg.username,
                "bash", "-lc",
                f"cd {build_chroot} && "
                "makepkg -s --skipchecksums --skipinteg --noconfirm --needed",
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"[CONFIGURE] makepkg build of sysforge failed "
                f"(exit {result.returncode}). Source remains at /root/sysforge "
                f"and build artefacts at {build_chroot} for inspection."
            )

        # Step 2: locate built packages (host-side glob) and install via pacman
        # with --overwrite so reinstalls over a prior uv-based install don't
        # fail on unowned /etc/sysforge/* files.
        built_pkgs = sorted(build_host.glob("*.pkg.tar.zst"))
        if not built_pkgs:
            raise RuntimeError(
                f"[CONFIGURE] makepkg reported success but no .pkg.tar.zst "
                f"found in {build_host}."
            )
        pkg_paths_chroot = [
            f"{build_chroot}/{p.name}" for p in built_pkgs
        ]
        result = _chroot(
            cfg.target,
            [
                "pacman", "-U",
                "--overwrite=/etc/sysforge/*",
                "--noconfirm",
                *pkg_paths_chroot,
            ],
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"[CONFIGURE] pacman -U install of sysforge failed "
                f"(exit {result.returncode}). Built packages remain at "
                f"{build_chroot} for inspection."
            )
    finally:
        sudoers_drop.unlink(missing_ok=True)

    _log.ui(f"sysforge {pkgver} installed via pacman (tracked).")


def _create_sysforge_group(cfg: BootstrapConfig) -> None:
    """Create the sysforge group and add the builder user to it."""
    _chroot(cfg.target, ["groupadd", "-f", "sysforge"])
    _chroot(cfg.target, ["usermod", "-aG", "sysforge", cfg.username])
    _log.ui(f"Group sysforge created, {cfg.username} added")


def _create_state_dir(cfg: BootstrapConfig) -> None:
    """Create /var/lib/sysforge owned by root:sysforge in the target.

    The bootstrap pipeline writes sysforge.log and pipeline_state.toml into
    this dir (as root) before the configure stage runs, so without a recursive
    pass those files stay root:root 0644 and block the post-reboot --resume
    when the primary user (in group sysforge) tries to append. Setgid on the
    dir means files written by stages after configure inherit the sysforge
    group automatically.
    """
    state_dir = Path(cfg.target) / "var/lib/sysforge"
    state_dir.mkdir(parents=True, exist_ok=True)
    # Pre-create sentinels/ so the libalpm hooks (shipped by the sysforge
    # PKGBUILD) can drop reminder files even before tmpfiles-create runs.
    (state_dir / "sentinels").mkdir(exist_ok=True)
    _chroot(cfg.target, ["chown", "-R", "root:sysforge", "/var/lib/sysforge"])
    _chroot(cfg.target, ["chmod", "02775", "/var/lib/sysforge"])
    _chroot(cfg.target, ["sh", "-c",
        "find /var/lib/sysforge -mindepth 1 -type d -exec chmod 02775 {} +; "
        "find /var/lib/sysforge -mindepth 1 -type f -exec chmod g+w {} +"])
    _log.ui("State dir: /var/lib/sysforge (root:sysforge, mode 02775, contents g+w)")


def _copy_config_files(cfg: BootstrapConfig) -> None:
    """Copy /etc/sysforge/ from the live ISO into the target system."""
    src = Path("/etc/sysforge")
    dst = Path(cfg.target) / "etc/sysforge"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    # bootstrap.toml holds plaintext root/user passwords; copytree preserves
    # the source mode (often 0644 from the ISO heredoc), so re-tighten here.
    bootstrap = dst / "bootstrap.toml"
    if bootstrap.exists():
        bootstrap.chmod(0o600)
    _log.ui("Config files copied to target /etc/sysforge/")


def _configure_desktop(cfg: BootstrapConfig) -> None:
    """Optionally select a desktop-environment package group.

    Resolution lives in :func:`pkg_catalog.select_desktop`: bootstrap.toml
    ``[desktop] environment`` wins non-interactively; otherwise a TTY run
    prompts; a non-TTY run with no preselection skips (so unattended installs
    never block). The chosen group is written into the *target's* packages.toml
    (already copied by :func:`_copy_config_files`) so the later packages stage
    installs it.
    """
    choice = select_desktop(interactive=is_interactive(), preselected=cfg.desktop)
    if not choice:
        _log.info("Desktop: none selected.")
        return
    pkgs_path = Path(cfg.target) / "etc/sysforge/packages.toml"
    write_desktop_group(pkgs_path, choice)
    _log.ui(f"Desktop: wrote [group.{choice}] to {pkgs_path} (installs in the packages stage).")


def _write_resume_reminder(cfg: BootstrapConfig) -> None:
    """Write a login-shell reminder to resume the pipeline after reboot."""
    dest = Path(cfg.target) / _RESUME_REMINDER_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_RESUME_REMINDER)
    dest.chmod(0o644)
    _log.ui("Resume reminder written to /etc/profile.d/sysforge-resume.sh (mode 0644)")


def _configure_shell(cfg: BootstrapConfig) -> None:
    """Write shell dotfiles for root and the primary user."""
    # root: red prompt
    root_dir = Path(cfg.target) / "root"
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / ".bashrc").write_text(
        _BASHRC + r"PS1='\[\e[1;31m\][\u@\h \W]\$\[\e[0m\] '" + "\n"
    )
    (root_dir / ".zshrc").write_text(
        _ZSHRC + r"PROMPT='%B%F{red}[%n@%m %1~]%#%f%b '" + "\n"
    )

    # primary user: green prompt
    user_dir = Path(cfg.target) / "home" / cfg.username
    if user_dir.is_dir():
        (user_dir / ".bashrc").write_text(
            _BASHRC + r"PS1='\[\e[1;32m\][\u@\h \W]\$\[\e[0m\] '" + "\n"
        )
        (user_dir / ".zshrc").write_text(
            _ZSHRC + r"PROMPT='%B%F{green}[%n@%m %1~]%#%f%b '" + "\n"
        )

    _log.ui(f"Shell: dotfiles written for root and {cfg.username}")


def _set_default_shell(cfg: BootstrapConfig) -> None:
    """Set the login shell for root and the primary user."""
    if cfg.shell == "bash":
        _log.info("Shell: bash (default, no chsh needed)")
        return

    shell_path = f"/usr/bin/{cfg.shell}"

    # Verify shell exists in the chroot
    if not (Path(cfg.target) / shell_path.lstrip("/")).exists():
        _log.warn(
            f"{cfg.shell} not found at {shell_path} in chroot — "
            f"install it first (e.g. add to pacstrap packages)"
        )
        return

    for user in ("root", cfg.username):
        _chroot(cfg.target, ["chsh", "-s", shell_path, user])

    _log.ui(f"Default shell: {cfg.shell} (root + {cfg.username})")


def _set_root_password(cfg: BootstrapConfig) -> None:
    """Set the root password from bootstrap.toml, or warn if not configured."""
    if cfg.root_password:
        run_or_raise(
            ["arch-chroot", cfg.target, "chpasswd"],
            tag="CONFIGURE", operation="chpasswd",
            hint="root password not set",
            input=f"root:{cfg.root_password}\n",
        )
        _log.ui("Root password set.")
    else:
        _log.warn(
            "No root_password in bootstrap.toml — set it manually after reboot:\n"
            f"  arch-chroot {cfg.target} passwd root"
        )


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

class ConfigureStage(Stage):
    name = "configure"
    description = "Bootstrap configuration — hostname, locale, bootloader, services"
    depends_on = ["hardware"]

    def run(self, config, state, options):  # noqa: ARG002
        cfg = load_bootstrap()

        _log.ui(f"Configuring target: {cfg.target}")

        if options.dry_run:
            _log.ui(f"[dry-run] hostname:   {cfg.hostname}")
            _log.ui(f"[dry-run] locale:     {cfg.locale}")
            _log.ui(f"[dry-run] timezone:   {cfg.timezone}")
            _log.ui(f"[dry-run] keymap:     {cfg.keymap}")
            _log.ui(f"[dry-run] ParallelDownloads: {cfg.parallel_downloads}")
            if cfg.mirror_countries:
                _log.ui(f"[dry-run] reflector countries: {cfg.mirror_countries}")
            _log.ui("[dry-run] would install bootloader: systemd-boot")
            _log.ui("[dry-run] would enable: NetworkManager, sshd")
            _log.ui("[dry-run] would configure: PermitRootLogin yes")
            if cfg.root_password:
                _log.ui("[dry-run] would set root password from bootstrap.toml")
            else:
                _log.ui("[dry-run] no root_password — will warn at runtime")
            if cfg.shell != "bash":
                _log.ui(f"[dry-run] would set default shell: {cfg.shell}")
            _log.ui("[dry-run] would copy /etc/sysforge/ to target")
            if cfg.desktop:
                _log.ui(f"[dry-run] would select desktop: {cfg.desktop} (writes [group.{cfg.desktop}])")
            else:
                _log.ui("[dry-run] would prompt for a desktop environment (interactive only)")
            _log.ui("[dry-run] would create /var/lib/sysforge (mode 0777)")
            _log.ui("[dry-run] would build sysforge in target via makepkg and install with pacman -U (tracked)")
            _log.ui("[dry-run] would write resume reminder to /etc/profile.d/sysforge-resume.sh")
            return

        _set_hostname(cfg)
        _set_locale(cfg)
        _set_timezone(cfg)
        _set_keymap(cfg)
        _set_pacman_parallel_downloads(cfg)
        _run_reflector(cfg)
        _install_bootloader(cfg)
        _enable_services(cfg)
        _configure_sshd(cfg)
        _create_user(cfg)
        _create_sysforge_group(cfg)
        _configure_shell(cfg)
        _set_default_shell(cfg)
        _copy_config_files(cfg)
        _configure_desktop(cfg)
        _create_state_dir(cfg)
        # Write the resume reminder before the (potentially fragile) sysforge
        # install. If install fails, the user still has a login-time breadcrumb
        # pointing at `sysforge run pipeline --resume`.
        _write_resume_reminder(cfg)
        _install_sysforge(cfg)
        _set_root_password(cfg)

        _log.ui("Configure stage complete.")
