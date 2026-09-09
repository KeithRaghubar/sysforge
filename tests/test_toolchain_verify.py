# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""
toolchain/verify.py — post-install evidence that the new clang links.
"""
from pathlib import Path
from sysforge.pipeline.stages.toolchain.verify import _newest_so
from sysforge.pipeline.stages.toolchain.verify import _so_ver
from sysforge.pipeline.stages.toolchain.verify import dump_stage_dynsym_evidence
from unittest.mock import MagicMock
from unittest.mock import patch

from tests.toolchain_helpers import (  # noqa: F401 — autouse fixture
    _make_so,
    _toolchain_gates_clean,
)


def test_dump_stage_dynsym_evidence_writes_brick_diff(tmp_path):
    """Evidence is the @LLVM_*-versioned std symbols a consumer demands that the
    installed libLLVM does not provide (the brick), plus a note when staging3
    DOES export them (the shipped libLLVM diverged from what 4b linked against).
    """
    install_root = tmp_path / "root"
    _make_so(install_root, "libLLVM.so.22.1")
    _make_so(install_root, "libclang-cpp.so.22.1")
    staging3 = tmp_path / "stage3"
    _make_so(staging3, "libLLVM.so.22.1")

    def fake_run(cmd, **kwargs):
        so = cmd[3]
        undefined = "--undefined-only" in cmd
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "libclang-cpp" in so and undefined:
            result.stdout = (
                "                 U _ZNSt7providedSym@LLVM_22.1\n"
                "                 U _ZNSt7missingSym@LLVM_22.1\n"
            )
        elif str(staging3) in so and not undefined:
            result.stdout = "0000000000333333 W _ZNSt7missingSym@@LLVM_22.1\n"
        elif not undefined:  # installed libLLVM defined-only
            result.stdout = (
                "0000000000111111 T LLVMCreateModule\n"
                "0000000000222222 W _ZNSt7providedSym@@LLVM_22.1\n"
            )
        else:
            result.stdout = ""
        return result

    dest_dir = tmp_path / "state"
    with patch("subprocess.run", side_effect=fake_run):
        out = dump_stage_dynsym_evidence(staging3, dest_dir, install_root=install_root)

    assert out is not None
    assert out == dest_dir / "llvm_abi_hazard.log"
    text = out.read_text()
    assert "installed libLLVM:" in text
    assert "brick cause" in text
    assert "_ZNSt7missingSym@LLVM_22.1" in text
    # provided-and-demanded symbol is NOT flagged as missing
    assert "_ZNSt7missingSym@LLVM_22.1" in text
    assert "ARE exported by" in text  # staging3 had it → divergence note
    assert "_ZNSt7providedSym" in text  # full dump section present

def test_dump_stage_dynsym_evidence_returns_none_when_no_installed_libllvm(tmp_path):
    """No installed libLLVM under install_root → returns None, writes nothing."""
    install_root = tmp_path / "root"
    (install_root / "usr/lib").mkdir(parents=True)
    dest_dir = tmp_path / "state"
    out = dump_stage_dynsym_evidence(tmp_path / "stage3", dest_dir, install_root=install_root)
    assert out is None
    assert not (dest_dir / "llvm_abi_hazard.log").exists()

def test_dump_stage_dynsym_evidence_returns_none_when_dest_dir_none(tmp_path):
    """Unset state dir (None) → returns None instead of raising TypeError.

    Regression: the Gate-3 failure path passes ``options.state_dir`` which is
    None whenever --state-dir isn't on the CLI, crashing on ``Path(None)``.
    """
    install_root = tmp_path / "root"
    _make_so(install_root, "libLLVM.so.22.1")
    out = dump_stage_dynsym_evidence(tmp_path / "stage3", None, install_root=install_root)
    assert out is None

def test_dump_stage_dynsym_evidence_ignores_compat_libllvm(tmp_path):
    """A compat package's older libLLVM.so.<old> (e.g. llvm21-libs alongside
    llvm-libs) must NOT be picked as 'the installed libLLVM'. The lexical-first
    glob did exactly that and reported a false '0 NOT provided' all-clear while
    clang actually dangled against the real .22.1. The diff must run against the
    libLLVM the consumers link (matched by soname version), not the compat one.
    """
    install_root = tmp_path / "root"
    _make_so(install_root, "libLLVM.so.21.1")   # compat (llvm21-libs)
    _make_so(install_root, "libLLVM.so.22.1")   # real (llvm-libs)
    _make_so(install_root, "libclang-cpp.so.22.1")
    staging3 = tmp_path / "stage3"
    _make_so(staging3, "libLLVM.so.22.1")

    def fake_run(cmd, **kwargs):
        so = cmd[3]
        undefined = "--undefined-only" in cmd
        result = MagicMock()
        result.returncode = 0
        result.stderr = ""
        if "libclang-cpp" in so and undefined:
            result.stdout = "                 U _ZNSt7missingSym@LLVM_22.1\n"
        elif so.endswith("libLLVM.so.22.1") and not undefined:
            # The REAL libLLVM does not export the demanded symbol → brick.
            result.stdout = "0000000000111111 T LLVMCreateModule\n"
        elif so.endswith("libLLVM.so.21.1") and not undefined:
            # The compat libLLVM *does* export it — if (wrongly) selected, the
            # diff would show 0 missing and hide the brick.
            result.stdout = "0000000000222222 W _ZNSt7missingSym@@LLVM_21.1\n"
        else:
            result.stdout = ""
        return result

    dest_dir = tmp_path / "state"
    with patch("subprocess.run", side_effect=fake_run):
        out = dump_stage_dynsym_evidence(staging3, dest_dir, install_root=install_root)

    assert out is not None
    text = out.read_text()
    # The diff ran against the real .22.1 (matched to the consumer), not .21.1.
    assert "libLLVM.so.22.1" in text
    assert "brick cause" in text
    assert "_ZNSt7missingSym@LLVM_22.1" in text

def test_so_ver_parses_numeric_version():
    assert _so_ver(Path("libLLVM.so.22.1")) == (22, 1)
    assert _so_ver(Path("libLLVM.so.9.0")) == (9, 0)
    assert _so_ver(Path("libLLVM.so")) == ()          # bare dev symlink name
    assert _so_ver(Path("libfoo.so.1.2.3")) == (1, 2, 3)

def test_newest_so_picks_highest_not_lexical_first(tmp_path):
    """Numeric, not lexical: .22.1 wins over .21.1 and .9.0 (which sorts last
    lexically). Symlinks are skipped so the unversioned dev symlink never wins.
    """
    lib = tmp_path / "usr/lib"
    lib.mkdir(parents=True)
    for n in ("libLLVM.so.21.1", "libLLVM.so.22.1", "libLLVM.so.9.0"):
        (lib / n).touch()
    (lib / "libLLVM.so").symlink_to("libLLVM.so.22.1")
    assert _newest_so(tmp_path, "libLLVM.so.*").name == "libLLVM.so.22.1"

def test_newest_so_none_when_absent(tmp_path):
    (tmp_path / "usr/lib").mkdir(parents=True)
    assert _newest_so(tmp_path, "libLLVM.so.*") is None

def test_verify_llvm_install_clean_returns_no_issues():
    """All components agree on version, clang/lld run, targets present."""
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    pacman_output = "\n".join(
        f"{n} 22.0.1-1" for n in (
            "llvm", "llvm-libs", "clang", "lld", "compiler-rt",
        )
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        if cmd[0] == "clang":
            return MagicMock(returncode=0, stdout="clang version 22.0.1", stderr="")
        if cmd[0] == "ld.lld":
            return MagicMock(returncode=0, stdout="LLD 22.0.1", stderr="")
        if cmd[0] == "llvm-config":
            return MagicMock(returncode=0, stdout="X86 AMDGPU NVPTX\n", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        issues = verify_llvm_install(expected_targets=["X86", "AMDGPU"])
    assert issues == []

def test_verify_llvm_install_uses_ld_lld_not_generic_driver():
    """Gate 3 must probe ``ld.lld``, never bare ``lld``.

    Regression for the false-positive Gate-3 failure: ``lld`` is the generic
    multiplexer driver and exits 1 ("lld is a generic driver") when invoked
    without a flavor, so probing it would always fail a perfectly good
    toolchain. ``ld.lld`` is the GNU-compatible flavor that ``-fuse-ld=lld``
    actually resolves to. This fake models the real behavior; the verifier must
    never hit the exit-1 ``lld`` arm.
    """
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    pacman_output = "\n".join(
        f"{n} 22.0.1-1" for n in (
            "llvm", "llvm-libs", "clang", "lld", "compiler-rt",
        )
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        if cmd[0] == "clang":
            return MagicMock(returncode=0, stdout="clang version 22.0.1", stderr="")
        if cmd[0] == "lld":
            # Generic driver: refuses to run without a flavor.
            return MagicMock(
                returncode=1, stdout="",
                stderr="lld is a generic driver.\nInvoke ld.lld (Unix), ...",
            )
        if cmd[0] == "ld.lld":
            return MagicMock(
                returncode=0, stdout="LLD 22.0.1 (compatible with GNU linkers)",
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        issues = verify_llvm_install()
    assert issues == []

def test_verify_llvm_install_detects_version_mismatch():
    """Different versions across LLVM components → canonical mismatch issue."""
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    # llvm-libs at 22.0.1, the rest at 22.0.0 — the exact failure mode
    # observed in practice (interrupted Pass-1 install of llvm-libs).
    pacman_output = (
        "llvm 22.0.0-1\n"
        "llvm-libs 22.0.1-1\n"
        "clang 22.0.0-1\n"
        "lld 22.0.0-1\n"
        "compiler-rt 22.0.0-1\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        issues = verify_llvm_install()
    assert any("versions disagree" in i for i in issues)
    assert any("llvm-libs=22.0.1-1" in i for i in issues)

def test_verify_llvm_install_detects_missing_target():
    """expected_targets not a subset of llvm-config output → issue raised."""
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    pacman_output = "\n".join(
        f"{n} 22.0.0-1" for n in (
            "llvm", "llvm-libs", "clang", "lld", "compiler-rt",
        )
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        if cmd[0] == "llvm-config":
            # X86 built, but AMDGPU and NVPTX were configured-out
            return MagicMock(returncode=0, stdout="X86\n", stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        issues = verify_llvm_install(expected_targets=["X86", "AMDGPU", "NVPTX"])
    assert any("missing expected backends" in i for i in issues)
    assert any("AMDGPU" in i for i in issues)

def test_verify_llvm_install_detects_crashing_clang():
    """clang --version exits non-zero (e.g. missing libLLVM.so) → issue."""
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    pacman_output = "\n".join(
        f"{n} 22.0.0-1" for n in (
            "llvm", "llvm-libs", "clang", "lld", "compiler-rt",
        )
    )

    def fake_run(cmd, **kwargs):
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        if cmd[0] == "clang":
            return MagicMock(
                returncode=127, stdout="",
                stderr="clang: error while loading shared libraries: libLLVM.so.22",
            )
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        issues = verify_llvm_install()
    assert any("clang --version" in i for i in issues)

def test_verify_llvm_install_skips_targets_when_none_configured():
    """No expected_targets → llvm-config not queried."""
    from sysforge.pipeline.stages.toolchain import verify_llvm_install

    pacman_output = "\n".join(
        f"{n} 22.0.0-1" for n in (
            "llvm", "llvm-libs", "clang", "lld", "compiler-rt",
        )
    )

    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd[0])
        if cmd[0] == "pacman":
            return MagicMock(returncode=0, stdout=pacman_output, stderr="")
        return MagicMock(returncode=0, stdout="ok", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run):
        verify_llvm_install(expected_targets=None)
    assert "llvm-config" not in calls

def test_llvm_recovery_command_lists_all_components():
    from sysforge.pipeline.stages.toolchain import llvm_recovery_command
    cmd = llvm_recovery_command()
    for pkg in ("llvm", "llvm-libs", "clang", "lld", "compiler-rt"):
        assert pkg in cmd
    assert cmd.startswith("sudo pacman -S ")

def test_check_llvm_link_resolution_clean_returns_no_issues():
    """ldd resolves libLLVM under /usr/lib for clang & lld → no issues."""
    from sysforge.pipeline.stages.toolchain import check_llvm_link_resolution

    ldd_clang = (
        "\tlinux-vdso.so.1 (0x00007ffd)\n"
        "\tlibLLVM-22.so => /usr/lib/libLLVM-22.so (0x00007f01)\n"
        "\tlibstdc++.so.6 => /usr/lib/libstdc++.so.6 (0x00007f00)\n"
    )
    ldd_lld = (
        "\tlibLLVM-22.so => /usr/lib/libLLVM-22.so (0x00007f01)\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["ldd"] and cmd[1] == "/usr/bin/clang":
            return MagicMock(returncode=0, stdout=ldd_clang, stderr="")
        if cmd[:1] == ["ldd"] and cmd[1] == "/usr/bin/lld":
            return MagicMock(returncode=0, stdout=ldd_lld, stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run), \
         patch("sysforge.pipeline.stages.toolchain.verify.Path.exists", return_value=True):
        issues = check_llvm_link_resolution()
    assert issues == []

def test_check_llvm_link_resolution_detects_staging_leak():
    """libLLVM resolves from /var/tmp/sysforge-llvm-stage* → issue raised.

    This is the failure mode F3 guards against: a Pass-4 packaging mistake
    leaves clang with an RPATH or symlink that points back at the staging
    prefix; /usr looks fine until /var/tmp gets cleaned and clang stops
    loading. Catch it in the verify step before the user sees a broken
    toolchain.
    """
    from sysforge.pipeline.stages.toolchain import check_llvm_link_resolution

    ldd_clang = (
        "\tlibLLVM-22.so => /var/tmp/sysforge-llvm-stage2/usr/lib/libLLVM-22.so "
        "(0x00007f01)\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["ldd"] and cmd[1] == "/usr/bin/clang":
            return MagicMock(returncode=0, stdout=ldd_clang, stderr="")
        if cmd[:1] == ["ldd"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run), \
         patch("sysforge.pipeline.stages.toolchain.verify.Path.exists", return_value=True):
        issues = check_llvm_link_resolution()
    assert any("staging prefix" in i for i in issues)
    assert any("/var/tmp/sysforge-llvm-stage2" in i for i in issues)

def test_check_llvm_link_resolution_detects_non_usr_lib():
    """libLLVM resolves outside /usr/lib (e.g. ~/.local) → issue raised."""
    from sysforge.pipeline.stages.toolchain import check_llvm_link_resolution

    ldd_clang = (
        "\tlibLLVM-22.so => /home/user/.local/lib/libLLVM-22.so (0x00007f01)\n"
    )

    def fake_run(cmd, **kwargs):
        if cmd[:1] == ["ldd"] and cmd[1] == "/usr/bin/clang":
            return MagicMock(returncode=0, stdout=ldd_clang, stderr="")
        if cmd[:1] == ["ldd"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout="", stderr="")

    with patch("sysforge.primitives.run.capture", side_effect=fake_run), \
         patch("sysforge.pipeline.stages.toolchain.verify.Path.exists", return_value=True):
        issues = check_llvm_link_resolution()
    assert any("outside /usr/lib" in i for i in issues)
