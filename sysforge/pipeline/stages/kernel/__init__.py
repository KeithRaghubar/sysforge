# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
stages/kernel/ — stage 8: kernel build

Builds a custom kernel from a PKGBUILD and runs post-install steps.

kernel.toml is loaded from /etc/sysforge/kernel.toml.

If kernel.toml is absent the stage exits cleanly — systems using a stock
kernel (installed via packages stage) skip this without needing --start-from.

kernel.toml structure:
  pkgname          = "linux-sysforge"
  pkgbuild_src_dir = "~/src"       # parent directory that contains the pkgname/ PKGBUILD dir
                                   # PKGBUILD is expected at pkgbuild_src_dir/pkgname/PKGBUILD
  bootloader       = "systemd-boot"    # systemd-boot | grub | none
  source           = "local"           # "local" | "aur" | "git" — PKGBUILD origin.
                                       # "local" (default) means hand-maintained, no
                                       # remote to sync from. Set to "aur"/"git" if the
                                       # kernel PKGBUILD is a clone of an AUR/git remote.

  device_kconfig   = true          # merge device-driven [kconfig_devices]
                                   # entries from hardware_profile.toml into
                                   # the fragment (default true)

  [[kconfig]]                      # manual kconfig overrides (optional)
  option = "CONFIG_HZ_1000"        # must match CONFIG_[A-Z0-9_]+
  value  = "y"                     # y | m | n | non-empty string (for string/int options)

kconfig fragment:
  Hardware-driven kconfig comes from hardware_profile.toml [kconfig] table,
  and device-driven entries from its [kconfig_devices] table (both emitted by
  the hardware stage; the latter gated by device_kconfig above). Manual
  overrides from kernel.toml [[kconfig]] are merged on top — precedence is
  manual > hardware > device; manual wins on conflict with a WARN.

  Gate 2 harvests the just-built tree's kbuild module→CONFIG_* map (the
  resolved .config's parent is the version-exact source tree) into
  <state_dir>/kbuild_module_map.json — the hardware stage loads that cache to
  widen [kconfig_devices] beyond device_probe's curated table on later runs.

  The combined fragment is written to <pkgbuild_src_dir>/<pkgname>/sysforge.config
  before makepkg runs. The PKGBUILD must merge this into its .config;
  a compatible PKGBUILD calls scripts/kconfig/merge_config.sh in prepare().

  If neither source provides any kconfig entries, no fragment is written.

base_config (optional, default "pkgbuild"):
  Selects the build's *starting* .config, before the fragment is overlaid:
  "pkgbuild" (the PKGBUILD's own base, no seeding), "running" (the running
  kernel's config from /proc/config.gz or /boot/config-$(uname -r)), or a path.
  For "running"/<path>, the chosen config is written to
  <pkgbuild_src_dir>/<pkgname>/sysforge.base.config; the PKGBUILD's prepare()
  must copy it to .config (then `make olddefconfig`) before merging
  sysforge.config — the same cooperation contract as the fragment.

Manual override validation:
  - option: must match CONFIG_[A-Z0-9_]+
  - value:  must be y, m, n, or a non-empty string (string/int options)
  - duplicates within kernel.toml: error
  - conflict with hardware_profile kconfig: warn, manual wins

Post-install steps (run after makepkg succeeds):
  1. sudo mkinitcpio -P   (always)
  2. Bootloader update    (configured via bootloader = ...)

Module layout (3.2.0-F2)
------------------------
This was one 2355-line module with a 567-line KernelStage. It is now a package
following the same pattern as the toolchain stage, and the seams the original
already had as comment banners:

  constants.py  valid-value contracts and the canonical recovery command
  config.py     kernel.toml -> KernelConfig, plus the precedence-bearing resolvers
  source.py     locating and syncing the PKGBUILD tree, and the pkgname checks
  fdo.py        sample-based FDO (AutoFDO / Propeller) orchestration
  kconfig.py    authoring the kconfig fragment the PKGBUILD merges
  install.py    initramfs and bootloader, the steps that make a kernel bootable
  gates.py      Gate 1/2/3 and the kconfig drift check
  stage.py      KernelStage: sequencing only, delegating every step

Dependencies run one way and ``stage.py`` is the only module that imports most
of the others. Modules call each other module-qualified (``kconfig.write_kconfig_fragment(...)``,
not a bare imported name), so a patch on the owning module is seen by every
caller. Names that cross a module boundary are public. This package's import
path is unchanged, and the names below are re-exported for callers that used the
flat module.
"""

from sysforge.pipeline.stages.kernel import (  # noqa: F401
    constants,
    config,
    source,
    fdo,
    kconfig,
    install,
    gates,
    stage,
)
from sysforge.pipeline.stages.kernel.constants import (  # noqa: F401
    VALID_SOURCES,
    SYNC_BLOCKING_STATUSES,
    VALID_BOOTLOADERS,
    VALID_COMPILERS,
    kernel_recovery_command,
)
from sysforge.pipeline.stages.kernel.config import (  # noqa: F401
    load_kernel_config,
    pkgbuild_path,
    resolve_bootloader,
    resolve_compiler,
    resolve_keep_hotplug_drivers,
    resolve_names,
    resolve_source,
    resolve_subpackages,
    srcdir_path,
)
from sysforge.pipeline.stages.kernel.source import (  # noqa: F401
    check_pkgname_repo_collision,
    presync_kernel_source,
    probe_installed_bootloader,
    resolve_already_built_action,
    validate_pkgname_matches_pkgbuild,
    warn_and_confirm_diverged,
)
from sysforge.pipeline.stages.kernel.fdo import (  # noqa: F401
    fdo_is_llvm,
    gate_fdo_llvm,
    resolve_fdo,
    run_fdo_capture,
)
from sysforge.pipeline.stages.kernel.kconfig import (  # noqa: F401
    HOTPLUG_KCONFIG,
    KCONFIG_ALL_TARGETS,
    KCONFIG_PROMPTING_TARGETS,
    KCONFIG_SILENT_EQUIVALENT,
    KCONFIG_SILENT_TARGETS,
    KCONFIG_UI_TARGETS,
    capture_lsmod_snapshot,
    format_kconfig_line,
    load_hardware_kconfig,
    merge_lsmod,
    resolve_base_config,
    resolve_kconfig_targets,
    validate_manual_kconfig,
    write_base_config,
    write_hotplug_fragment,
    write_kconfig_fragment,
)
from sysforge.pipeline.stages.kernel.install import (  # noqa: F401
    run_mkinitcpio,
    update_bootloader,
)
from sysforge.pipeline.stages.kernel.gates import (  # noqa: F401
    KCONFIG_DIFF_CAP,
    built_kernel_release,
    gate1_preflight,
    gate2_audit,
    gate2_kconfig_drift,
    gate3_verify,
    kconfig_diff_lines,
    kconfig_drift_lines,
    record_and_diff_kconfig,
    resolve_built_config,
)
from sysforge.pipeline.stages.kernel.stage import (  # noqa: F401
    KernelStage,
    log_resolution_summary,
)

__all__ = [
    "KCONFIG_DIFF_CAP",
    "HOTPLUG_KCONFIG",
    "KCONFIG_ALL_TARGETS",
    "KCONFIG_PROMPTING_TARGETS",
    "KCONFIG_SILENT_EQUIVALENT",
    "KCONFIG_SILENT_TARGETS",
    "KCONFIG_UI_TARGETS",
    "KernelStage",
    "SYNC_BLOCKING_STATUSES",
    "VALID_BOOTLOADERS",
    "VALID_COMPILERS",
    "VALID_SOURCES",
    "built_kernel_release",
    "capture_lsmod_snapshot",
    "check_pkgname_repo_collision",
    "fdo_is_llvm",
    "format_kconfig_line",
    "gate1_preflight",
    "gate2_audit",
    "gate2_kconfig_drift",
    "gate3_verify",
    "gate_fdo_llvm",
    "kconfig_diff_lines",
    "kconfig_drift_lines",
    "kernel_recovery_command",
    "load_hardware_kconfig",
    "load_kernel_config",
    "log_resolution_summary",
    "merge_lsmod",
    "pkgbuild_path",
    "presync_kernel_source",
    "probe_installed_bootloader",
    "record_and_diff_kconfig",
    "resolve_already_built_action",
    "resolve_base_config",
    "resolve_bootloader",
    "resolve_built_config",
    "resolve_compiler",
    "resolve_fdo",
    "resolve_kconfig_targets",
    "resolve_keep_hotplug_drivers",
    "resolve_names",
    "resolve_source",
    "resolve_subpackages",
    "run_fdo_capture",
    "run_mkinitcpio",
    "srcdir_path",
    "update_bootloader",
    "validate_manual_kconfig",
    "validate_pkgname_matches_pkgbuild",
    "warn_and_confirm_diverged",
    "write_base_config",
    "write_hotplug_fragment",
    "write_kconfig_fragment",
    "constants",
    "config",
    "source",
    "fdo",
    "kconfig",
    "install",
    "gates",
    "stage",
]
