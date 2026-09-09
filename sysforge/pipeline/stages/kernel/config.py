# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
kernel/config.py — kernel.toml parsed once, into a typed shape.

``KernelConfig.from_toml`` is the single place kernel.toml's raw dict becomes
values the stage uses. Before 3.2.0-F6 the stage carried 28 ``kernel_cfg.get``
calls, so each consumer re-derived the same defaults and a mistyped key
surfaced as a ``None`` mid-build rather than at stage entry.

The resolvers here are the ones with real precedence rules — CLI flag over
kernel.toml over pipeline state, and a name pair that has to stay consistent
with the PKGBUILD. They stay functions because their inputs include ``options``
and ``state``, which are not config.
"""
from dataclasses import dataclass, field
from pathlib import Path
import tomllib

from sysforge.primitives.paths import KERNEL_PATH

from sysforge.pipeline.stages.kernel import constants

from sysforge import log

_log = log.get_logger("KERNEL")


def resolve_compiler(kernel_cfg, options, state):
    """
    Resolve the kernel-stage compiler. Precedence:
      1. options.compiler (CLI --compiler)
      2. kernel_cfg["compiler"]
      3. pipeline state's toolchain.cc (mapped back to "gcc" or "llvm")
      4. None (let makepkg_wrapper fall through to profile defaults)

    Returns ("gcc"|"llvm"|None, cc_path|None, cxx_path|None).
    """
    cli = getattr(options, "compiler", None)
    cfg = kernel_cfg.compiler
    for source, val in (("--compiler", cli), ("kernel.toml", cfg)):
        if val and val not in constants.VALID_COMPILERS:
            raise RuntimeError(
                f"[KERNEL] invalid {source} value {val!r}: "
                f"must be one of {constants.VALID_COMPILERS}"
            )

    compiler = cli or cfg
    if compiler:
        # identity.py is the toolchain stage's register-only half — the
        # compiler-path lookup that does no building (3.2.0-F2). This used to
        # reach for a private name on the flat toolchain module.
        from sysforge.pipeline.stages.toolchain.identity import compiler_paths
        cc, cxx, _ = compiler_paths(compiler)
        return compiler, cc, cxx

    toolchain = state.get_stage_result("toolchain") if state else None
    cc = toolchain.get("cc") if toolchain else None
    cxx = toolchain.get("cxx") if toolchain else None
    return None, cc, cxx


def resolve_subpackages(kernel_cfg, options):
    """Resolve whether to build the kernel -headers / -docs subpackages.

    Precedence per toggle: CLI flag (``--headers``/``--no-headers``,
    ``--docs``/``--no-docs``) > kernel.toml (``build_headers``/``build_docs``) >
    hard default (headers on, docs off). The CLI fields default to ``None`` when
    the flag is unset, so ``None`` falls through to the TOML value.

    Returns ``(build_headers, build_docs)``.
    """
    cli_headers = getattr(options, "build_headers", None)
    build_headers = (
        cli_headers if cli_headers is not None
        else kernel_cfg.build_headers
    )
    cli_docs = getattr(options, "build_docs", None)
    build_docs = (
        cli_docs if cli_docs is not None
        else kernel_cfg.build_docs
    )
    return build_headers, build_docs


def resolve_keep_hotplug_drivers(kernel_cfg, options):
    """Resolve whether to re-enable hotplug driver classes as modules after
    config minimization (F2).

    Precedence: CLI (``--keep-hotplug-drivers`` / ``--no-keep-hotplug-drivers``)
    > kernel.toml (``keep_hotplug_drivers``) > hard default (off). The CLI field
    defaults to ``None`` when the flag is unset, so ``None`` falls through to the
    TOML value.
    """
    cli = getattr(options, "keep_hotplug_drivers", None)
    if cli is not None:
        return bool(cli)
    return kernel_cfg.keep_hotplug_drivers


def resolve_bootloader(kernel_cfg, options):
    """Resolve bootloader: CLI override > kernel.toml > 'systemd-boot' default."""
    cli = getattr(options, "bootloader", None)
    if cli and cli not in constants.VALID_BOOTLOADERS:
        raise RuntimeError(
            f"[KERNEL] invalid --bootloader value {cli!r}: "
            f"must be one of {constants.VALID_BOOTLOADERS}"
        )
    cfg = kernel_cfg.bootloader
    if cfg not in constants.VALID_BOOTLOADERS:
        raise RuntimeError(
            f"[KERNEL] invalid kernel.toml bootloader {cfg!r}: "
            f"must be one of {constants.VALID_BOOTLOADERS}"
        )
    return cli or cfg

def resolve_names(kernel_cfg):
    """Resolve ``(upstream_pkgname, pkgname)`` from kernel.toml (F40).

    ``upstream_pkgname`` is what sysforge pulls/tracks (e.g. ``linux-zen``);
    ``pkgname`` is the local name it builds/installs as, defaulting to
    ``upstream_pkgname`` when omitted. Pure-local configs set only ``pkgname``
    (upstream is None → no sync remote, no rename). At least one must be set.
    """
    upstream = kernel_cfg.upstream_pkgname
    pkgname = kernel_cfg.pkgname or upstream
    if not pkgname:
        raise RuntimeError(
            "[KERNEL] kernel.toml is missing pkgname (set pkgname, or "
            "upstream_pkgname to track an upstream kernel)."
        )
    return upstream, pkgname


def resolve_source(kernel_cfg, srcdir_path):
    """Resolve the kernel PKGBUILD source classification (F40).

    Explicit ``source`` (``local`` | ``repo`` | ``aur``) is honored — ``git``
    was a phantom value (no URL field ever existed) and now yields a clear
    error. When omitted, auto-resolve ``local → repo → aur``:

    * ``srcdir_path`` exists without ``.git`` → hand-maintained tree →
      ``local`` (never clobbered by a re-clone).
    * ``srcdir_path`` is an existing git clone → ``repo``: the scheduler's
      generic fetch path rebases via the tree's own origin (and skips the
      AUR RPC, which is wrong for non-AUR upstreams).
    * ``srcdir_path`` missing → pick the clone remote by whether the tracked
      name is in a pacman sync DB (the same probe the pkgname-collision
      check uses): in a repo → ``repo`` (pkgctl), else → ``aur``.
    """
    src = kernel_cfg.source
    if src is not None:
        if src == "git":
            raise RuntimeError(
                "[KERNEL] kernel.toml source = \"git\" is no longer supported "
                "(it never had a URL to clone from). Use \"local\", \"repo\", "
                "or \"aur\" — or omit source to auto-resolve."
            )
        if src not in constants.VALID_SOURCES:
            raise RuntimeError(
                f"[KERNEL] invalid kernel.toml source {src!r}: "
                f"must be one of {constants.VALID_SOURCES}"
            )
        return src

    srcdir_path = Path(srcdir_path)
    if srcdir_path.is_dir():
        if (srcdir_path / ".git").exists():
            return "repo"
        return "local"

    from sysforge.primitives.aur import is_repo_package
    upstream, pkgname = resolve_names(kernel_cfg)
    tracked = upstream or pkgname
    return "repo" if is_repo_package(tracked) else "aur"

def load_kernel_config():
    """
    Load kernel.toml. Returns the parsed dict, or None if the file does not
    exist (making the stage a no-op).
    """
    path = KERNEL_PATH

    if not path.exists():
        return None

    with path.open("rb") as f:
        data = tomllib.load(f)

    _log.info(f"Loaded kernel config from {path}")
    return data


def srcdir_path(kernel_cfg):
    """Resolve the kernel PKGBUILD *directory* (no existence requirement).

    Split out of :func:`pkgbuild_path` so the build entry can hand the dir to
    the source-sync scheduler *before* requiring a PKGBUILD — a missing tree is
    then bootstrapped by the scheduler's clone-if-missing path (F40) instead of
    aborting here.
    """
    # KernelStage.run() stamps the effective value (kernel.toml override, else
    # the global [paths] pkgbuild_src_dir) into kernel_cfg before this is called,
    # so an empty value here means neither source is set.
    pkgbuild_src_dir = kernel_cfg.pkgbuild_src_dir
    if not pkgbuild_src_dir:
        raise RuntimeError(
            "[KERNEL] no pkgbuild_src_dir configured. Set [paths] pkgbuild_src_dir "
            "in profiles.toml (the global default) or pkgbuild_src_dir in kernel.toml "
            "(per-kernel override) to the directory that contains your kernel PKGBUILD "
            'directory (e.g. "~/src" if the PKGBUILD is at ~/src/linux-custom/PKGBUILD).'
        )
    upstream, pkgname = resolve_names(kernel_cfg)
    srcdir = kernel_cfg.srcdir or upstream or pkgname
    return Path(pkgbuild_src_dir).expanduser() / srcdir


def pkgbuild_path(kernel_cfg):
    """
    Resolve the PKGBUILD for the configured kernel package.
    Returns Path to the PKGBUILD file.

    Looks for <pkgbuild_src_dir>/<srcdir>/PKGBUILD where srcdir resolves as
    srcdir → upstream_pkgname → pkgname (first set wins): the explicit
    override, else the tracked upstream's name, else the local name. srcdir
    allows the source directory name to differ from either (e.g.
    pkgname="linux-custom", srcdir="linux").
    """
    src_dir = srcdir_path(kernel_cfg)
    candidate = src_dir / "PKGBUILD"
    if not candidate.exists():
        if src_dir.is_dir():
            # Directory exists but the PKGBUILD is gone — the signature of an
            # interrupted `--cleansrc` purge (purge_src rmtree's the tree, then
            # the scheduler re-clones; an interruption in between leaves this
            # half-removed state). Recoverable: re-run with --cleansrc to
            # re-clone. See DESIGN.md §Kernel stage source sync.
            raise RuntimeError(
                f"[KERNEL] PKGBUILD missing from existing dir {src_dir} — "
                "an interrupted --cleansrc may have left a partial tree. "
                "Re-run with --cleansrc to re-clone, or restore the PKGBUILD."
            )
        raise RuntimeError(
            f"[KERNEL] PKGBUILD not found: {candidate}. "
            f"Clone the kernel PKGBUILD into {src_dir!r} first."
        )
    return candidate


# ---------------------------------------------------------------------------
# lsmod snapshot
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Typed config (3.2.0-F6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KernelConfig:
    """kernel.toml, parsed once at stage entry.

    The counterpart to ``ToolchainConfig``, and frozen for the same reason: a
    stage's configuration is decided before it starts and must not drift
    mid-run. TOML keys are unchanged; this is the in-process representation.

    Only the *plain* settings live here. The resolvers above stay functions
    because their inputs are not all config — ``resolve_compiler`` weighs a CLI
    flag against kernel.toml against pipeline state, ``resolve_source`` probes
    the filesystem, and ``resolve_names`` derives a pair that has to agree with
    the PKGBUILD. Turning those into fields would mean computing them at parse
    time, before ``options`` and ``state`` exist.

    ``raw`` is kept for ``config.resolve_pkgbuild_src_dir(config, build_cfg=raw)``
    and for the resolvers, which still take the table.
    """

    raw: dict = field(default_factory=dict, repr=False)

    enabled: bool = False
    interactive: bool = True
    # None means 'unset', which is load-bearing: resolve_compiler falls
    # through to the pipeline state's toolchain result only when the user
    # did not name a compiler here.
    compiler: str | None = None

    # Naming: the three keys that decide what is built and where from.
    pkgname: str | None = None
    upstream_pkgname: str | None = None
    srcdir: str | None = None
    source: str | None = None
    pkgbuild_src_dir: str | None = None

    # Build products
    build_headers: bool = True
    build_docs: bool = False

    # kconfig authoring
    base_config: str = "pkgbuild"
    kconfig_merge: bool = True
    kconfig_targets: list | None = None
    device_kconfig: bool = True
    keep_hotplug_drivers: bool = False
    capture_lsmod_snapshot: bool = True
    manual_kconfig: tuple = ()

    # Boot-safety gates
    boot_audit: bool = True
    require_fallback_kernel: bool = True
    min_boot_free_mb: int = 200
    bootloader: str = "systemd-boot"

    @classmethod
    def from_toml(cls, data: dict | None) -> "KernelConfig | None":
        """Build the typed config from kernel.toml's parsed dict.

        Returns ``None`` for ``None`` (no kernel.toml — the stage is a clean
        no-op), so the caller's absent-file branch is unchanged.
        """
        if data is None:
            return None
        return cls(
            raw=data,
            enabled=bool(data.get("enabled", False)),
            interactive=bool(data.get("interactive", True)),
            compiler=data.get("compiler") or None,
            pkgname=data.get("pkgname") or None,
            upstream_pkgname=data.get("upstream_pkgname") or None,
            srcdir=data.get("srcdir") or None,
            source=data.get("source") or None,
            pkgbuild_src_dir=data.get("pkgbuild_src_dir") or None,
            build_headers=bool(data.get("build_headers", True)),
            build_docs=bool(data.get("build_docs", False)),
            base_config=str(data.get("base_config", "pkgbuild")),
            kconfig_merge=bool(data.get("kconfig_merge", True)),
            kconfig_targets=data.get("kconfig_targets"),
            device_kconfig=bool(data.get("device_kconfig", True)),
            keep_hotplug_drivers=bool(data.get("keep_hotplug_drivers", False)),
            capture_lsmod_snapshot=bool(data.get("capture_lsmod_snapshot", True)),
            manual_kconfig=tuple(data.get("kconfig", []) or ()),
            boot_audit=bool(data.get("boot_audit", True)),
            require_fallback_kernel=bool(data.get("require_fallback_kernel", True)),
            min_boot_free_mb=int(data.get("min_boot_free_mb", 200)),
            bootloader=str(data.get("bootloader", "systemd-boot")),
        )


def load() -> "KernelConfig | None":
    """Read kernel.toml and return it typed — the stage's entry point.

    ``load_kernel_config`` remains as the raw-dict reader beneath this.
    """
    return KernelConfig.from_toml(load_kernel_config())
