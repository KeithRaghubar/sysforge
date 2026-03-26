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
  [system] root_password       string  root password set via chpasswd (prompted if absent)
  [mirror] countries           list    reflector --country values
  [mirror] protocol            string  reflector --protocol (default: "https")
  [mirror] age                 int     reflector --latest N (default: 12)

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

import sysforge.log as _log
from sysforge.pipeline.stages.base import Stage
from sysforge.pipeline.stages._bootstrap import load_bootstrap, BootstrapConfig


# ---------------------------------------------------------------------------
# arch-chroot helper
# ---------------------------------------------------------------------------

def _chroot(target: str, cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a command inside arch-chroot <target>."""
    full_cmd = ["arch-chroot", target] + cmd
    _log.info("[CONFIGURE]", f"chroot: {' '.join(cmd)}")
    return subprocess.run(full_cmd, check=check)


# ---------------------------------------------------------------------------
# Configuration steps
# ---------------------------------------------------------------------------

def _set_hostname(cfg: BootstrapConfig) -> None:
    hostname_file = Path(cfg.target) / "etc/hostname"
    hostname_file.write_text(cfg.hostname + "\n")
    _log.ui("[CONFIGURE]", f"Hostname: {cfg.hostname}")


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
            _log.info("[CONFIGURE]", f"Uncommented {cfg.locale} in /etc/locale.gen")
        else:
            _log.warn("[CONFIGURE]", f"{cfg.locale} not found in /etc/locale.gen — locale-gen may fail")
    else:
        _log.warn("[CONFIGURE]", f"{cfg.target}/etc/locale.gen not found — skipping locale.gen edit")

    # Write locale.conf
    locale_conf = Path(cfg.target) / "etc/locale.conf"
    locale_conf.write_text(f"LANG={cfg.locale}\n")
    _log.ui("[CONFIGURE]", f"Locale: {cfg.locale}")

    # Run locale-gen inside chroot
    _chroot(cfg.target, ["locale-gen"])


def _set_timezone(cfg: BootstrapConfig) -> None:
    # Create /etc/localtime symlink via timedatectl or direct ln -sf
    tz_path = f"/usr/share/zoneinfo/{cfg.timezone}"
    _chroot(cfg.target, ["ln", "-sf", tz_path, "/etc/localtime"])
    _chroot(cfg.target, ["hwclock", "--systohc"])
    _log.ui("[CONFIGURE]", f"Timezone: {cfg.timezone}")


def _set_keymap(cfg: BootstrapConfig) -> None:
    if not cfg.keymap or cfg.keymap == "us":
        # us is the kernel default; vconsole.conf is only needed for non-default
        _log.info("[CONFIGURE]", "Keymap: us (default, skipping vconsole.conf)")
        return
    vconsole = Path(cfg.target) / "etc/vconsole.conf"
    vconsole.write_text(f"KEYMAP={cfg.keymap}\n")
    _log.ui("[CONFIGURE]", f"Keymap: {cfg.keymap}")


def _set_pacman_parallel_downloads(cfg: BootstrapConfig) -> None:
    n = cfg.parallel_downloads
    pacman_conf = Path(cfg.target) / "etc/pacman.conf"
    if not pacman_conf.exists():
        _log.warn("[CONFIGURE]", f"{pacman_conf} not found — skipping ParallelDownloads")
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
        _log.ui("[CONFIGURE]", f"ParallelDownloads: {n}")


def _run_reflector(cfg: BootstrapConfig) -> None:
    """Run reflector inside the chroot to generate an optimised mirrorlist."""
    # Check reflector is available in the chroot
    result = _chroot(cfg.target, ["which", "reflector"], check=False)
    if result.returncode != 0:
        _log.warn(
            "[CONFIGURE]",
            "reflector not found in chroot — skipping mirrorlist update. "
            "Install reflector and re-run, or update /etc/pacman.d/mirrorlist manually.",
        )
        return

    cmd = ["reflector", "--protocol", cfg.mirror_protocol,
           "--latest", str(cfg.mirror_age),
           "--sort", "rate",
           "--save", "/etc/pacman.d/mirrorlist"]

    if cfg.mirror_countries:
        for country in cfg.mirror_countries:
            cmd += ["--country", country]

    _log.ui("[CONFIGURE]", f"Running reflector: {' '.join(cmd[1:])}")
    result = _chroot(cfg.target, cmd, check=False)
    if result.returncode != 0:
        _log.warn(
            "[CONFIGURE]",
            f"reflector exited {result.returncode} — mirrorlist may be unchanged",
        )
    else:
        _log.ui("[CONFIGURE]", "Mirrorlist updated.")


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
    _log.ui("[CONFIGURE]", "Bootloader: systemd-boot installed")


def _enable_services(cfg: BootstrapConfig) -> None:
    """Enable NetworkManager and sshd so they start on first boot."""
    _chroot(cfg.target, ["systemctl", "enable", "NetworkManager"])
    _chroot(cfg.target, ["systemctl", "enable", "sshd"])
    _log.ui("[CONFIGURE]", "Services enabled: NetworkManager, sshd")


def _configure_sshd(cfg: BootstrapConfig) -> None:
    """Allow root login via SSH (required for initial access)."""
    sshd_config = Path(cfg.target) / "etc/ssh/sshd_config"
    if not sshd_config.exists():
        _log.warn("[CONFIGURE]", "sshd_config not found — skipping PermitRootLogin config")
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
    _log.ui("[CONFIGURE]", "sshd: PermitRootLogin yes")


def _create_user(cfg: BootstrapConfig) -> None:
    """Create the primary user, add to wheel, and configure sudo."""
    # Create user with home dir and wheel group membership
    result = _chroot(cfg.target, ["useradd", "-m", "-G", "wheel", cfg.username], check=False)
    if result.returncode not in (0, 9):  # 9 = already exists
        raise RuntimeError(f"[CONFIGURE] useradd failed for {cfg.username!r} (exit {result.returncode})")
    _log.ui("[CONFIGURE]", f"User: {cfg.username} (wheel)")

    # Allow wheel group to use sudo via a sudoers drop-in
    sudoers_d = Path(cfg.target) / "etc/sudoers.d"
    sudoers_d.mkdir(parents=True, exist_ok=True)
    (sudoers_d / "wheel").write_text("%wheel ALL=(ALL:ALL) ALL\n")
    _log.ui("[CONFIGURE]", "sudo: wheel group enabled")

    if cfg.user_password:
        result = subprocess.run(
            ["arch-chroot", cfg.target, "chpasswd"],
            input=f"{cfg.username}:{cfg.user_password}\n",
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"[CONFIGURE] chpasswd failed for {cfg.username!r}")
        _log.ui("[CONFIGURE]", f"Password set for {cfg.username}.")
    else:
        _log.warn(
            "[CONFIGURE]",
            f"No user_password in bootstrap.toml — set it manually after reboot:\n"
            f"  passwd {cfg.username}",
        )


_BASHRC = (
    "[[ $- != *i* ]] && return\n"
    "\n"
    "alias ls='ls --color=auto'\n"
    "alias grep='grep --color=auto'\n"
    "\n"
)
_ZSHRC = (
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


def _find_sysforge_source() -> Path | None:
    """
    Return the sysforge source directory as recorded by pip's direct_url.json.
    Returns None if the package was not installed from a local path or if the
    path no longer exists.
    """
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
    return None


def _install_sysforge(cfg: BootstrapConfig) -> None:
    """Copy the sysforge source from the live ISO into the target and install it."""
    src = _find_sysforge_source()
    if src is None:
        raise RuntimeError(
            "[CONFIGURE] Cannot locate sysforge source via pip metadata — "
            "cannot install sysforge into target."
        )

    target_src = Path(cfg.target) / "root/sysforge"
    if target_src.exists():
        shutil.rmtree(target_src)
    shutil.copytree(src, target_src)
    _log.ui("[CONFIGURE]", f"sysforge source copied to target ({src} → /root/sysforge)")

    _chroot(cfg.target, ["uv", "pip", "install", "--system", "--no-deps", "/root/sysforge"])
    _log.ui("[CONFIGURE]", "sysforge installed into target.")


def _copy_config_files(cfg: BootstrapConfig) -> None:
    """Copy /etc/sysforge/ from the live ISO into the target system."""
    src = Path("/etc/sysforge")
    dst = Path(cfg.target) / "etc/sysforge"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst, dirs_exist_ok=True)
    _log.ui("[CONFIGURE]", "Config files copied to target /etc/sysforge/")


def _write_resume_reminder(cfg: BootstrapConfig) -> None:
    """Write a login-shell reminder to resume the pipeline after reboot."""
    dest = Path(cfg.target) / _RESUME_REMINDER_PATH
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_RESUME_REMINDER)
    _log.ui("[CONFIGURE]", "Resume reminder written to /etc/profile.d/sysforge-resume.sh")


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

    _log.ui("[CONFIGURE]", f"Shell: dotfiles written for root and {cfg.username}")


def _set_root_password(cfg: BootstrapConfig) -> None:
    """Set the root password from bootstrap.toml, or warn if not configured."""
    if cfg.root_password:
        result = subprocess.run(
            ["arch-chroot", cfg.target, "chpasswd"],
            input=f"root:{cfg.root_password}\n",
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError("[CONFIGURE] chpasswd failed — root password not set")
        _log.ui("[CONFIGURE]", "Root password set.")
    else:
        _log.warn(
            "[CONFIGURE]",
            "No root_password in bootstrap.toml — set it manually after reboot:\n"
            f"  arch-chroot {cfg.target} passwd root",
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

        _log.ui("[CONFIGURE]", f"Configuring target: {cfg.target}")

        if options.dry_run:
            _log.ui("[CONFIGURE]", f"[dry-run] hostname:   {cfg.hostname}")
            _log.ui("[CONFIGURE]", f"[dry-run] locale:     {cfg.locale}")
            _log.ui("[CONFIGURE]", f"[dry-run] timezone:   {cfg.timezone}")
            _log.ui("[CONFIGURE]", f"[dry-run] keymap:     {cfg.keymap}")
            _log.ui("[CONFIGURE]", f"[dry-run] ParallelDownloads: {cfg.parallel_downloads}")
            if cfg.mirror_countries:
                _log.ui("[CONFIGURE]", f"[dry-run] reflector countries: {cfg.mirror_countries}")
            _log.ui("[CONFIGURE]", "[dry-run] would install bootloader: systemd-boot")
            _log.ui("[CONFIGURE]", "[dry-run] would enable: NetworkManager, sshd")
            _log.ui("[CONFIGURE]", "[dry-run] would configure: PermitRootLogin yes")
            if cfg.root_password:
                _log.ui("[CONFIGURE]", "[dry-run] would set root password from bootstrap.toml")
            else:
                _log.ui("[CONFIGURE]", "[dry-run] no root_password — will warn at runtime")
            _log.ui("[CONFIGURE]", "[dry-run] would copy /etc/sysforge/ to target")
            _log.ui("[CONFIGURE]", "[dry-run] would install sysforge into target via uv")
            _log.ui("[CONFIGURE]", "[dry-run] would write resume reminder to /etc/profile.d/sysforge-resume.sh")
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
        _configure_shell(cfg)
        _copy_config_files(cfg)
        _install_sysforge(cfg)
        _write_resume_reminder(cfg)
        _set_root_password(cfg)

        _log.ui("[CONFIGURE]", "Configure stage complete.")
