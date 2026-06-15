"""Direct unit tests for the stage-ownership registry primitive.

These drive the public API (``load_stage_ownership`` snapshot + ``owner_of`` /
``owned_pkgbases``) against real on-disk TOML, patching only the config-path
constants — the endorsed seam for stage config. They assert the *ownership
decision*, not implementation detail, so they survive the primitive's internals
changing.
"""

from __future__ import annotations

from unittest.mock import patch

from sysforge.primitives import stage_ownership
from sysforge.primitives.stage_ownership import (
    load_stage_ownership,
    owned_pkgbases,
    owner_of,
)


def _configs(tmp_path, *, kernel: str | None = None, toolchain: str | None = None):
    """Write optional kernel/toolchain TOML and patch the path constants to them.

    A None body means "config absent" — the path points at a nonexistent file.
    Returns the patch context manager (use with ``with``).
    """
    kpath = tmp_path / "kernel.toml"
    tpath = tmp_path / "toolchain.toml"
    if kernel is not None:
        kpath.write_text(kernel)
    if toolchain is not None:
        tpath.write_text(toolchain)
    return patch.multiple(
        stage_ownership,
        KERNEL_PATH=kpath,
        TOOLCHAIN_PATH=tpath,
    )


# ---------------------------------------------------------------------------
# Nothing owned
# ---------------------------------------------------------------------------

def test_no_configs_owns_nothing(tmp_path):
    with _configs(tmp_path):
        snap = load_stage_ownership()
    assert snap.any_active is False
    assert snap.owner_of("llvm") is None
    assert snap.owner_of("linux-custom") is None
    assert snap.owned_pkgbases() == set()


def test_malformed_toml_treated_as_absent(tmp_path):
    with _configs(tmp_path, kernel="this is = = not toml ["):
        snap = load_stage_ownership()
    assert snap.kernel_pkgbase is None
    assert snap.any_active is False


# ---------------------------------------------------------------------------
# Kernel ownership
# ---------------------------------------------------------------------------

def test_kernel_owns_its_pkgname(tmp_path):
    with _configs(tmp_path, kernel='enabled = true\npkgname = "linux-custom"\n'):
        snap = load_stage_ownership()
    assert snap.any_active is True
    assert snap.owner_of("linux-custom") == "kernel"
    assert snap.owned_pkgbases() == {"linux-custom"}


def test_kernel_owns_via_pkgbase_for_split_member(tmp_path):
    """A split member (name != pkgbase) is owned when its pkgbase matches."""
    with _configs(tmp_path, kernel='pkgname = "linux-custom"\n'):
        snap = load_stage_ownership()
    # e.g. linux-custom-headers built under pkgbase linux-custom
    assert snap.owner_of("linux-custom-headers", "linux-custom") == "kernel"


def test_kernel_config_without_pkgname_owns_nothing(tmp_path):
    with _configs(tmp_path, kernel="enabled = true\n"):
        snap = load_stage_ownership()
    assert snap.kernel_pkgbase is None
    assert snap.owner_of("linux-custom") is None


def test_kernel_unrelated_package_not_owned(tmp_path):
    with _configs(tmp_path, kernel='pkgname = "linux-custom"\n'):
        snap = load_stage_ownership()
    assert snap.owner_of("htop") is None


# ---------------------------------------------------------------------------
# Toolchain ownership
# ---------------------------------------------------------------------------

_TC_LLVM = 'enabled = true\ncompiler = "llvm"\n'


def test_toolchain_owns_llvm_prefix_suite(tmp_path):
    with _configs(tmp_path, toolchain=_TC_LLVM):
        snap = load_stage_ownership()
    assert snap.any_active is True
    for pkg in ("llvm", "clang", "lld", "compiler-rt"):
        assert snap.owner_of(pkg) == "toolchain", pkg


def test_toolchain_owns_split_member_via_pkgbase(tmp_path):
    """llvm-libs (pkgbase llvm) is owned through is_llvm_pkgbase on the base."""
    with _configs(tmp_path, toolchain=_TC_LLVM):
        snap = load_stage_ownership()
    assert snap.owner_of("llvm-libs", "llvm") == "toolchain"


def test_toolchain_owns_configured_non_prefix_package(tmp_path):
    """spirv-llvm-translator isn't an is_llvm_pkgbase prefix match, but is owned
    when listed in [packages]."""
    toml = _TC_LLVM + '[packages]\nnon_pgo = ["spirv-llvm-translator"]\n'
    with _configs(tmp_path, toolchain=toml):
        snap = load_stage_ownership()
    assert snap.owner_of("spirv-llvm-translator") == "toolchain"
    assert "spirv-llvm-translator" in snap.owned_pkgbases()


def test_toolchain_does_not_own_lib32_by_default(tmp_path):
    """lib32-* LLVM packages are NOT built by the toolchain stage by default, so
    the prefix match must not claim them — otherwise `update` would skip them
    with nothing building them. They flow through `sysforge update` instead."""
    with _configs(tmp_path, toolchain=_TC_LLVM):
        snap = load_stage_ownership()
    for pkg in ("lib32-llvm", "lib32-clang", "lib32-llvm-libs"):
        assert snap.owner_of(pkg) is None, pkg
    # The 64-bit suite is still owned.
    assert snap.owner_of("llvm") == "toolchain"


def test_toolchain_owns_lib32_when_explicitly_configured(tmp_path):
    """A user can still opt lib32 back into the toolchain pass via [packages]
    lib32 — then those names are toolchain-owned through the configured path."""
    toml = _TC_LLVM + '[packages]\nlib32 = ["lib32-llvm", "lib32-clang"]\n'
    with _configs(tmp_path, toolchain=toml):
        snap = load_stage_ownership()
    assert snap.owner_of("lib32-llvm") == "toolchain"
    assert snap.owner_of("lib32-clang") == "toolchain"
    # A lib32 package NOT listed is still not owned.
    assert snap.owner_of("lib32-llvm-libs") is None


def test_toolchain_gcc_compiler_owns_no_llvm(tmp_path):
    """Register-only gcc path builds no LLVM — stock pacman LLVM is left alone."""
    with _configs(tmp_path, toolchain='enabled = true\ncompiler = "gcc"\n'):
        snap = load_stage_ownership()
    assert snap.any_active is False
    assert snap.owner_of("llvm") is None


def test_toolchain_compiler_unset_defaults_gcc(tmp_path):
    """Unset compiler defaults to gcc (register-only) — owns no LLVM."""
    with _configs(tmp_path, toolchain="enabled = true\n"):
        snap = load_stage_ownership()
    assert snap.toolchain_active is False
    assert snap.owner_of("clang") is None


def test_toolchain_disabled_owns_no_llvm(tmp_path):
    with _configs(tmp_path, toolchain='enabled = false\ncompiler = "llvm"\n'):
        snap = load_stage_ownership()
    assert snap.owner_of("llvm") is None


def test_toolchain_owned_pkgbases_excludes_dynamic_prefix_set(tmp_path):
    """owned_pkgbases enumerates only configured names, not the unbounded
    is_llvm_pkgbase prefix set."""
    toml = _TC_LLVM + '[packages]\npgo = ["llvm"]\nlib32 = ["lib32-llvm"]\n'
    with _configs(tmp_path, toolchain=toml):
        snap = load_stage_ownership()
    assert snap.owned_pkgbases() == {"llvm", "lib32-llvm"}
    # clang is owned by prefix match but is NOT statically enumerable:
    assert snap.owner_of("clang") == "toolchain"
    assert "clang" not in snap.owned_pkgbases()


# ---------------------------------------------------------------------------
# Precedence + combined
# ---------------------------------------------------------------------------

def test_kernel_takes_precedence_over_toolchain(tmp_path):
    """A package matching the kernel pkgbase is kernel-owned even with an active
    toolchain stage (the two never overlap in practice, but order is fixed)."""
    with _configs(
        tmp_path,
        kernel='pkgname = "llvm"\n',  # contrived overlap
        toolchain=_TC_LLVM,
    ):
        snap = load_stage_ownership()
    assert snap.owner_of("llvm") == "kernel"


def test_both_stages_classify_their_own(tmp_path):
    with _configs(
        tmp_path,
        kernel='pkgname = "linux-custom"\n',
        toolchain=_TC_LLVM,
    ):
        snap = load_stage_ownership()
    assert snap.owner_of("linux-custom") == "kernel"
    assert snap.owner_of("clang") == "toolchain"
    assert snap.owner_of("htop") is None
    assert snap.owned_pkgbases() == {"linux-custom"}


# ---------------------------------------------------------------------------
# Module-level convenience wrappers
# ---------------------------------------------------------------------------

def test_module_level_owner_of_snapshots_per_call(tmp_path):
    with _configs(tmp_path, kernel='pkgname = "linux-custom"\n'):
        assert owner_of("linux-custom") == "kernel"
        assert owner_of("htop") is None


def test_module_level_owned_pkgbases(tmp_path):
    with _configs(tmp_path, toolchain=_TC_LLVM + '[packages]\npgo = ["llvm"]\n'):
        assert owned_pkgbases() == {"llvm"}
