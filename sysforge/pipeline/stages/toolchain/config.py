# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/config.py — toolchain.toml parsed once, into a typed shape.

``ToolchainConfig.from_toml`` is the single place where toolchain.toml's raw
dict becomes values the rest of the package uses: defaults applied, lists
normalised, unknown training-corpus entries rejected. Before 3.2.0-F6 the stage
carried 27 ``cfg.get(...)`` calls, so a mistyped key surfaced as a ``None``
deep inside a gate rather than as an error at stage entry, and each consumer
re-derived the same defaults.

The dataclass is frozen: a stage's configuration is decided at entry and does
not drift mid-run. TOML keys are unchanged — this is the in-process
representation only.
"""
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from sysforge.primitives.paths import TOOLCHAIN_PATH  # noqa: F401 — re-exported
from sysforge.primitives.paths import resolve_packages_path

from sysforge.pipeline.stages.toolchain import constants

from sysforge.primitives import config as prim_config
from sysforge import log

_log = log.get_logger("TOOLCHAIN")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_toolchain_config() -> dict | None:
    """
    Load toolchain.toml. Returns None if absent (stage is a no-op).
    Raises RuntimeError on TOML parse failure.
    """
    if not TOOLCHAIN_PATH.exists():
        return None
    try:
        with TOOLCHAIN_PATH.open("rb") as f:
            return tomllib.load(f)
    except Exception as e:
        raise RuntimeError(
            f"[TOOLCHAIN] Failed to parse {TOOLCHAIN_PATH}: {e}"
        ) from None


def resolve_packages_repo_mode(config: dict) -> str:
    """Read packages.toml ``[build] repo_mode`` (the single read chokepoint).

    Returns ``"pacman"`` or ``"build_from_source"`` via
    :func:`config.resolve_repo_mode`. A missing/unreadable packages.toml falls
    back to the documented default (``"pacman"``) — that is the correct default
    for the repo-install branch (install the stock LLVM suite rather than build).
    """
    try:
        path = resolve_packages_path(config)
        with path.open("rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return prim_config.REPO_MODE_PACMAN
    return prim_config.resolve_repo_mode(data.get("build", {}))


def package_lists(tcfg: dict) -> tuple[list[str], list[str], list[str]]:
    """
    Return (pgo_pkgs, non_pgo_pkgs, lib32_pkgs) for the LLVM toolchain build.

    Only called on the LLVM path — the GCC path short-circuits to register
    system gcc paths without building anything (stock `gcc`/`gcc-libs` from
    pacman/base-devel provide the runtime).
    """
    pkgs_cfg = tcfg.get("packages", {})
    pgo_pkgs = pkgs_cfg.get("pgo", constants.DEFAULT_LLVM_PGO)
    non_pgo_pkgs = pkgs_cfg.get("non_pgo", constants.DEFAULT_LLVM_NON_PGO)
    lib32_pkgs = pkgs_cfg.get("lib32", constants.DEFAULT_LLVM_LIB32)
    return pgo_pkgs, non_pgo_pkgs, lib32_pkgs


SONAME_MODES = ("prompt", "auto", "off")


def _soname_mode(raw) -> str:
    """Validate ``rebuild_soname_consumers``, warning rather than failing.

    An unrecognised value falls back to ``"prompt"`` — the mode that never
    silently breaks the system — because a typo in an advisory knob should not
    abort a toolchain build. Warning at parse time is the improvement: before
    3.2.0-F6 an unknown value simply matched none of the gate's branches and
    took the prompt path with nothing said.
    """
    if raw is None or raw == "":
        return "prompt"
    mode = str(raw)
    if mode not in SONAME_MODES:
        _log.warn(
            f"Unknown rebuild_soname_consumers={mode!r} in toolchain.toml — "
            f"using 'prompt' (known: {', '.join(SONAME_MODES)})",
        )
        return "prompt"
    return mode


# Known training-corpus members. "llvm" is the implicit base — the 4-pass build
# compiles LLVM's own source regardless, so listing it is a no-op. Extra members
# (currently only "mesa") are compiled by the instrumented stage1 clang during
# Pass 3 *purely to enrich clang.profdata* with non-LLVM codegen patterns; they
# are never installed and never become -fprofile-use targets (the profile stays
# clang-keyed). See DESIGN.md §Flag/Profile System (training corpus).
_KNOWN_CORPUS = frozenset({"llvm", "mesa"})
_DEFAULT_TRAINING_CORPUS = ["llvm"]


def resolve_training_corpus(tcfg: dict) -> list[str]:
    """Return the *extra* (non-llvm) training-corpus package names, ordered.

    Reads ``[packages] training_corpus`` (default ``["llvm"]``). "llvm" is the
    implicit base and is stripped from the result; unknown members are warned
    and dropped; duplicates are collapsed. The returned list is exactly what
    Pass 3 additionally compiles with the instrumented stage1 clang so their
    codegen lands in ``clang.profdata``. An empty list means "LLVM self-build
    only" — the historical behaviour.
    """
    raw = tcfg.get("packages", {}).get("training_corpus", _DEFAULT_TRAINING_CORPUS)
    if isinstance(raw, str):
        raw = [raw]
    extras: list[str] = []
    for name in raw:
        if name == "llvm":
            continue  # implicit base; not an "extra"
        if name not in _KNOWN_CORPUS:
            _log.warn(
                f"[PGO] Unknown training_corpus member {name!r} — ignoring "
                f"(known: {', '.join(sorted(_KNOWN_CORPUS))})",
            )
            continue
        if name not in extras:
            extras.append(name)
    return extras


def bolt_config(tcfg: dict) -> dict:
    """Return the ``[bolt]`` config with defaults applied (LLVM/PGO Pass 5).

    Keys: ``enabled`` (default false — opt-in), ``libllvm`` (also BOLT
    libLLVM.so, default false — the shared lib is more fragile than the clang
    executable), ``training_workload`` (path to a .cpp profiled to collect the
    BOLT profile; empty → a generated header-heavy TU). One read home so the
    Pass-4 emit-relocs gate and the Pass-5 orchestration agree on the same flags.
    """
    bcfg = tcfg.get("bolt", {}) or {}
    return {
        "enabled": bool(bcfg.get("enabled", False)),
        "libllvm": bool(bcfg.get("libllvm", False)),
        "training_workload": str(bcfg.get("training_workload", "") or ""),
    }


# ---------------------------------------------------------------------------
# Typed config (3.2.0-F6)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BoltConfig:
    """The ``[bolt]`` section — Pass 5, opt-in post-link optimization.

    ``libllvm`` defaults off separately from ``enabled`` because BOLTing the
    shared library is materially more fragile than BOLTing the clang
    executable, and a user opting into the latter has not opted into the former.
    """

    enabled: bool = False
    libllvm: bool = False
    training_workload: str = ""


@dataclass(frozen=True)
class ToolchainConfig:
    """toolchain.toml, parsed once at stage entry.

    Frozen because a stage's configuration is decided before it starts and must
    not drift mid-run — with a dict, any consumer could have written back into
    it. The TOML key names are unchanged; this is the in-process representation.

    Every default lives here, exactly once. That is the point: the same key used
    to be defaulted independently at each read site, and the defaults had
    already diverged — ``compiler`` fell back to ``"gcc"`` in the stage body and
    to ``"llvm"`` in the Gate-1 sentinel, so a config with no ``compiler`` key
    got a sentinel stamped with the wrong toolchain.

    ``raw`` is the undecoded dict, kept for exactly two consumers, both of which
    take the whole table by design:

    * ``makepkg_pgo.resolve_pgo_store(raw)`` — the pgo_store resolver is shared
      with the wrapper's orphan-profraw guard and owns its own precedence
      (config > ``SYSFORGE_PGO_STORE`` > ``/var/cache``);
    * the Pass-4 reuse ``config_digest``, which hashes the toolchain settings
      wholesale. It must keep hashing the raw table — hashing a field list
      instead would change every existing cache key and silently invalidate
      every user's reuse cache.
    """

    raw: dict = field(default_factory=dict, repr=False)

    enabled: bool = False
    compiler: str = "gcc"
    pgo: bool = True
    skip_build: bool = False
    reuse_unchanged: bool = False
    drift_detect: str = "fingerprint"

    # Gate 1
    require_multilib: bool = True
    min_build_free_gb: float = 40.0
    # Three-valued mode, not a flag: "prompt" (default) | "auto" | "off".
    # A CLI flag still outranks it; the default lives here so the gate does
    # not carry its own fallback.
    rebuild_soname_consumers: str = "prompt"

    # Staging prefixes for the multi-pass build
    staging1: Path = Path(constants.DEFAULT_STAGING_1)
    staging: Path = Path(constants.DEFAULT_STAGING)
    staging3: Path = Path(constants.DEFAULT_STAGING_3)

    # [packages]
    pgo_pkgs: tuple[str, ...] = ()
    non_pgo_pkgs: tuple[str, ...] = ()
    lib32_pkgs: tuple[str, ...] = ()
    training_corpus: tuple[str, ...] = ()

    bolt: BoltConfig = field(default_factory=BoltConfig)

    @property
    def pgo_enabled(self) -> bool:
        """``pgo`` is only meaningful on the LLVM path.

        The GCC path never builds anything, so ``pgo = true`` there is not an
        error to reject but a setting with nothing to apply to. Deriving it here
        rather than at each use is what stops one call site from reading the raw
        ``pgo`` flag and concluding a gcc run is a PGO run.
        """
        return bool(self.pgo) if self.compiler == "llvm" else False

    @classmethod
    def from_toml(cls, data: dict | None) -> "ToolchainConfig | None":
        """Build the typed config from toolchain.toml's parsed dict.

        Returns ``None`` for ``None`` (no toolchain.toml — the stage is a clean
        no-op), so the caller's absent-file branch is unchanged.
        """
        if data is None:
            return None
        pkgs = data.get("packages", {}) or {}
        bcfg = bolt_config(data)
        return cls(
            raw=data,
            enabled=bool(data.get("enabled", False)),
            compiler=str(data.get("compiler", "gcc")),
            pgo=bool(data.get("pgo", True)),
            skip_build=bool(data.get("skip_build", False)),
            reuse_unchanged=bool(data.get("reuse_unchanged", False)),
            drift_detect=str(data.get("drift_detect", "fingerprint")),
            require_multilib=bool(data.get("require_multilib", True)),
            min_build_free_gb=float(data.get("min_build_free_gb", 40)),
            rebuild_soname_consumers=_soname_mode(data.get("rebuild_soname_consumers")),
            staging1=Path(data.get("pgo_staging1", constants.DEFAULT_STAGING_1)),
            staging=Path(data.get("pgo_staging", constants.DEFAULT_STAGING)),
            staging3=Path(data.get("pgo_staging3", constants.DEFAULT_STAGING_3)),
            pgo_pkgs=tuple(pkgs.get("pgo", constants.DEFAULT_LLVM_PGO)),
            non_pgo_pkgs=tuple(pkgs.get("non_pgo", constants.DEFAULT_LLVM_NON_PGO)),
            lib32_pkgs=tuple(pkgs.get("lib32", constants.DEFAULT_LLVM_LIB32)),
            training_corpus=tuple(resolve_training_corpus(data)),
            bolt=BoltConfig(**bcfg),
        )


def load() -> "ToolchainConfig | None":
    """Read toolchain.toml and return it typed — the stage's entry point.

    ``load_toolchain_config`` remains as the raw-dict reader beneath this, since
    the parse-failure message and the absent-file contract belong there.
    """
    return ToolchainConfig.from_toml(load_toolchain_config())
