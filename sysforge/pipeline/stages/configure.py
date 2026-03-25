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

import subprocess
import re
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


def _configure_shell(cfg: BootstrapConfig) -> None:
    """Write /root/.bashrc and /root/.zshrc with colored prompts and common aliases."""
    root_dir = Path(cfg.target) / "root"
    root_dir.mkdir(parents=True, exist_ok=True)

    (root_dir / ".bashrc").write_text(
        "[[ $- != *i* ]] && return\n"
        "\n"
        "alias ls='ls --color=auto'\n"
        "alias grep='grep --color=auto'\n"
        "\n"
        r"PS1='\[\e[1;31m\][\u@\h \W]\$\[\e[0m\] '" + "\n"
    )

    (root_dir / ".zshrc").write_text(
        "alias ls='ls --color=auto'\n"
        "alias grep='grep --color=auto'\n"
        "\n"
        "setopt autocd\n"
        "\n"
        r"PROMPT='%B%F{red}[%n@%m %1~]%#%f%b '" + "\n"
    )

    _log.ui("[CONFIGURE]", "Shell: .bashrc and .zshrc written (colored prompts)")


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
        _configure_shell(cfg)
        _set_root_password(cfg)

        _log.ui("[CONFIGURE]", "Configure stage complete.")
