"""
test_log_tags.py — registry of expected module-level logger tags.

Each assertion verifies that the named Logger in a given module was created
with the correct tag string.  These tests fail until the logging migration
(get_logger refactor) is complete — that is intentional.

Tag decisions recorded here:
  - cli._log           [CLI]          (was [BUILD] — wrong subsystem)
  - state._log         [STATE]        (was [CONFIG] — collision with config.py)
  - pkgbuild_patcher   [PATCH] only   (stray [BUILD] on line 115 fixed to [PATCH])
  - toolchain._log     [TOOLCHAIN]    ([PGO] appears only in message text, not as tag)

Multi-tag modules use named loggers: _<tag>_log (e.g. _conf_log, _build_log).
"""
import sysforge.cli as cli
import sysforge.converge as converge
import sysforge.fetch as fetch
import sysforge.packages_cmd as packages_cmd
import sysforge.pipeline.runner as runner
import sysforge.pipeline.stages.base_install as base_install
import sysforge.pipeline.stages.configure as configure
import sysforge.pipeline.stages.hardware as hardware
import sysforge.pipeline.stages.kernel as kernel
import sysforge.pipeline.stages.packages as packages
import sysforge.pipeline.stages.partition as partition
import sysforge.pipeline.stages.reconfigure as reconfigure
import sysforge.pipeline.stages.toolchain as toolchain
import sysforge.pipeline.state as state
import sysforge.primitives.abi_check as abi_check
import sysforge.primitives.aur as aur
import sysforge.primitives.cache_probe as cache_probe
import sysforge.primitives.config as config
import sysforge.primitives.dep_analysis as dep_analysis
import sysforge.primitives.failure as failure
import sysforge.primitives.makepkg_conf as makepkg_conf
import sysforge.primitives.makepkg_env as makepkg_env
import sysforge.primitives.makepkg_flags as makepkg_flags
import sysforge.primitives.makepkg_wrapper as makepkg_wrapper
import sysforge.primitives.pacman as pacman
import sysforge.primitives.pkgbuild_patcher as pkgbuild_patcher
import sysforge.primitives.profile as profile
import sysforge.primitives.version as version
import sysforge.update as update


# ---------------------------------------------------------------------------
# Single-tag modules — module-level _log
# ---------------------------------------------------------------------------

def test_cli_tag():              assert cli._log._tag              == "[CLI]"
def test_converge_tag():         assert converge._log._tag         == "[CONVERGE]"
def test_fetch_tag():            assert fetch._log._tag            == "[FETCH]"
def test_update_tag():           assert update._log._tag           == "[UPDATE]"
def test_packages_cmd_tag():     assert packages_cmd._log._tag     == "[PACKAGES]"
def test_runner_tag():           assert runner._log._tag           == "[PIPELINE]"
def test_state_tag():            assert state._log._tag            == "[STATE]"
def test_base_install_tag():     assert base_install._log._tag     == "[BASE_INSTALL]"
def test_configure_tag():        assert configure._log._tag        == "[CONFIGURE]"
def test_hardware_tag():         assert hardware._log._tag         == "[HARDWARE]"
def test_kernel_stage_tag():     assert kernel._log._tag           == "[KERNEL]"
def test_packages_stage_tag():   assert packages._log._tag         == "[PACKAGES]"
def test_partition_tag():        assert partition._log._tag        == "[PARTITION]"
def test_reconfigure_tag():      assert reconfigure._log._tag      == "[RECONFIGURE]"
def test_toolchain_tag():        assert toolchain._log._tag        == "[TOOLCHAIN]"
def test_abi_check_tag():        assert abi_check._log._tag        == "[ABI]"
def test_cache_probe_tag():      assert cache_probe._log._tag      == "[CACHE]"
def test_config_tag():           assert config._log._tag           == "[CONFIG]"
def test_dep_analysis_tag():     assert dep_analysis._log._tag     == "[DEP]"
def test_failure_tag():          assert failure._log._tag          == "[FAILURE]"
def test_pacman_tag():           assert pacman._log._tag           == "[PACMAN]"
def test_pkgbuild_patcher_tag(): assert pkgbuild_patcher._log._tag == "[PATCH]"
def test_version_tag():          assert version._log._tag          == "[VERSION]"


# ---------------------------------------------------------------------------
# Multi-tag modules — named loggers (_<tag>_log)
# ---------------------------------------------------------------------------

def test_makepkg_flags_tag():
    # P2b.1: flag-string manipulation extracted to makepkg_flags (owns [FLAG]).
    assert makepkg_flags._flag_log._tag == "[FLAG]"


def test_makepkg_env_tag():
    # P2b.4: env-var resolution extracted to makepkg_env (owns [ENV]).
    assert makepkg_env._env_log._tag == "[ENV]"


def test_makepkg_conf_tags():
    # P2b.4: emit_makepkg_conf relocated to makepkg_conf (owns [CONF]). It still
    # emits [FLAG]/[PGO]/[KERNEL] inline for conf-specific decisions until the
    # collapse step folds those into their owning modules.
    assert makepkg_conf._conf_log._tag   == "[CONF]"
    assert makepkg_conf._flag_log._tag   == "[FLAG]"
    assert makepkg_conf._pgo_log._tag    == "[PGO]"
    assert makepkg_conf._kernel_log._tag == "[KERNEL]"


def test_makepkg_wrapper_tags():
    # ABI / CACHE / PATCH emission relocated to abi_check / cache_probe /
    # pkgbuild_patcher (P2a); CONF relocated to makepkg_conf (P2b.4). The
    # remaining tags are the orchestrator's own + the not-yet-split sites.
    assert makepkg_wrapper._build_log._tag   == "[BUILD]"
    assert makepkg_wrapper._env_log._tag     == "[ENV]"
    assert makepkg_wrapper._flag_log._tag    == "[FLAG]"
    assert makepkg_wrapper._git_log._tag     == "[GIT]"
    assert makepkg_wrapper._kernel_log._tag  == "[KERNEL]"
    assert makepkg_wrapper._makepkg_log._tag == "[MAKEPKG]"
    assert makepkg_wrapper._pgo_log._tag     == "[PGO]"


def test_makepkg_wrapper_relocated_tags_gone():
    # Guard the relocations: relocated loggers must not reappear in the orchestrator.
    assert not hasattr(makepkg_wrapper, "_abi_log")    # P2a
    assert not hasattr(makepkg_wrapper, "_cache_log")  # P2a
    assert not hasattr(makepkg_wrapper, "_patch_log")  # P2a
    assert not hasattr(makepkg_wrapper, "_conf_log")   # P2b.4


def test_profile_tags():
    assert profile._conf_log._tag    == "[CONF]"
    assert profile._flag_log._tag    == "[FLAG]"
    assert profile._groups_log._tag  == "[GROUPS]"
    assert profile._profile_log._tag == "[PROFILE]"


def test_aur_tags():
    assert aur._aur_log._tag      == "[AUR]"
    assert aur._build_log._tag    == "[BUILD]"
    assert aur._git_log._tag      == "[GIT]"
    assert aur._manifest_log._tag == "[MANIFEST]"
