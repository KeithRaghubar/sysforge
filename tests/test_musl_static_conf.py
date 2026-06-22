"""
test_musl_static_conf.py — emit_makepkg_conf(is_musl_static=True) flag scrub.

Static-musl bootstraps (pacman-static) build their bundled libs with
CC=musl-gcc + -static. The sysforge profile's lld linker (-fuse-ld=lld) +
-static + musl produces a startup-crashing binary (configure's conftest
segfaults), and musl-gcc cannot consume a clang .profdata. emit_makepkg_conf
must force the bfd linker and scrub PGO flags for these builds — the musl
analogue of the is_lib32 scrub.

Mirrors the lib32 scrub coverage in test_lib32_pipeline.py: a positive path,
a non-musl control, and gcc/clang PGO parity.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sysforge.primitives.makepkg_wrapper import emit_makepkg_conf
from sysforge.primitives.profile import merge_extends

# Minimal profile set; the lld linker is declared in the *system* conf (as it
# is in /etc/makepkg-clang.conf on the real workstation), so the bare profile
# is enough to exercise the system-conf passthrough scrub.
PROFILES = {
    "bare": {"BUILDDIR": "$HOME/builds"},
    "clang": {"extends": "bare", "CC": "clang", "CXX": "clang++"},
    "gcc": {"extends": "bare", "CC": "gcc", "CXX": "g++"},
}

_PGO_FLAG = "-fprofile-use=/var/cache/sysforge/llvm-pgo/clang.profdata"


def _write_system_conf(tmp_path, ldflags):
    p = tmp_path / "system-makepkg.conf"
    p.write_text(
        'CARCH="x86_64"\n'
        'CHOST="x86_64-pc-linux-gnu"\n'
        'CFLAGS="-march=native -O2 -pipe"\n'
        'CXXFLAGS="-march=native -O2 -pipe"\n'
        f'LDFLAGS="{ldflags}"\n'
    )
    return p


def _ldflags_line(text):
    return next(
        (ln for ln in text.splitlines() if ln.startswith("LDFLAGS=")), ""
    )


def _emit(tmp, profile_name, *, is_musl_static, ldflags, compiler_flags_extra=None):
    system_conf = _write_system_conf(tmp, ldflags)
    resolved = merge_extends(profile_name, PROFILES, conflict_groups={})
    with emit_makepkg_conf(
        resolved, frozenset({"makepkg", "env"}),
        system_conf_path=str(system_conf),
        is_musl_static=is_musl_static,
        compiler_flags_extra=compiler_flags_extra,
    ) as conf_path:
        return Path(conf_path).read_text()


def test_musl_static_forces_bfd_linker():
    """-fuse-ld=lld in system LDFLAGS is replaced with bfd for a musl build."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "clang", is_musl_static=True,
                     ldflags="-fuse-ld=lld -Wl,-O1,--as-needed")
    line = _ldflags_line(text)
    assert "-fuse-ld=lld" not in line
    assert "-fuse-ld=bfd" in line
    # Non-lld content survives.
    assert "--as-needed" in line


def test_musl_static_strips_lld_only_flags():
    """lld-only --icf tokens are stripped even when nested in -Wl,..."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "clang", is_musl_static=True,
                     ldflags="-fuse-ld=lld -Wl,--icf=all,--as-needed")
    line = _ldflags_line(text)
    assert "--icf=all" not in line
    assert "--as-needed" in line


def test_non_musl_keeps_lld_linker():
    """Default is_musl_static=False leaves the lld linker untouched."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "clang", is_musl_static=False,
                     ldflags="-fuse-ld=lld -Wl,-O1,--as-needed")
    assert "-fuse-ld=lld" in _ldflags_line(text)


def test_musl_static_no_lld_declared_is_noop():
    """No -fuse-ld in LDFLAGS → nothing forced; bfd is the compiler default."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "gcc", is_musl_static=True,
                     ldflags="-Wl,-O1,--as-needed")
    line = _ldflags_line(text)
    assert "-fuse-ld=" not in line
    assert "--as-needed" in line


def test_musl_static_scrubs_pgo_flag_clang_path():
    """clang profile: injected -fprofile-use is scrubbed from all flag vars."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "clang", is_musl_static=True,
                     ldflags="-fuse-ld=lld", compiler_flags_extra=_PGO_FLAG)
    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        line = next((ln for ln in text.splitlines() if ln.startswith(f"{key}=")), "")
        assert "-fprofile-use" not in line, f"{key} still carries PGO flag: {line}"


def test_musl_static_scrubs_pgo_flag_gcc_path():
    """gcc profile: the PGO scrub is compiler-agnostic and still fires."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "gcc", is_musl_static=True,
                     ldflags="-fuse-ld=lld", compiler_flags_extra=_PGO_FLAG)
    for key in ("CFLAGS", "CXXFLAGS", "LDFLAGS"):
        line = next((ln for ln in text.splitlines() if ln.startswith(f"{key}=")), "")
        assert "-fprofile-use" not in line, f"{key} still carries PGO flag: {line}"


def test_non_musl_keeps_pgo_flag():
    """Control: without is_musl_static the injected PGO flag survives."""
    with tempfile.TemporaryDirectory() as d:
        text = _emit(Path(d), "clang", is_musl_static=False,
                     ldflags="-fuse-ld=lld", compiler_flags_extra=_PGO_FLAG)
    assert "-fprofile-use" in _ldflags_line(text)
