"""
test_os_release.py — distro identity from ``os-release(5)`` (doctor ``distro`` axis).

Every case drives the parser through an explicit ``paths`` tuple pointing at
tmp_path files, so no test reads the host's real ``/etc/os-release`` — the suite
must produce the same result on Arch, on a derivative, and in a container.
"""
from __future__ import annotations

from pathlib import Path

from sysforge.primitives import diagnostics as diag
from sysforge.primitives import os_release

ARCH = 'NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\nID=arch\nBUILD_ID=rolling\n'

DERIVATIVE = (
    'NAME="CachyOS Linux"\n'
    'PRETTY_NAME="CachyOS"\n'
    'ID=cachyos\n'
    'ID_LIKE=arch\n'
)

FOREIGN = 'NAME="Debian GNU/Linux"\nID=debian\nID_LIKE=""\n'


def _write(tmp_path: Path, text: str, name: str = "os-release") -> tuple[Path, ...]:
    p = tmp_path / name
    p.write_text(text)
    return (p,)


# --- parsing ----------------------------------------------------------------

def test_read_parses_quoted_and_bare_values(tmp_path):
    fields = os_release.read_os_release(_write(tmp_path, ARCH))
    assert fields["NAME"] == "Arch Linux"      # double quotes stripped
    assert fields["ID"] == "arch"              # bare value untouched
    assert fields["BUILD_ID"] == "rolling"


def test_read_ignores_comments_and_blank_lines(tmp_path):
    text = "# a comment\n\nID=arch\n   \n#ID=bogus\n"
    fields = os_release.read_os_release(_write(tmp_path, text))
    assert fields["ID"] == "arch"


def test_read_strips_single_quotes(tmp_path):
    fields = os_release.read_os_release(_write(tmp_path, "ID='arch'\n"))
    assert fields["ID"] == "arch"


def test_read_falls_through_to_second_path(tmp_path):
    """os-release(5) puts the vendor copy at /usr/lib; only /etc may be absent."""
    usr = tmp_path / "usr-os-release"
    usr.write_text(ARCH)
    fields = os_release.read_os_release((tmp_path / "missing", usr))
    assert fields["ID"] == "arch"
    assert fields["_source"] == str(usr)


def test_read_returns_empty_when_no_path_readable(tmp_path):
    assert os_release.read_os_release((tmp_path / "nope",)) == {}


def test_read_treats_content_free_file_as_unreadable(tmp_path):
    """A truncated os-release must not become ID=linux via the spec default."""
    assert os_release.read_os_release(_write(tmp_path, "# only a comment\n")) == {}


# --- identity ---------------------------------------------------------------

def test_identify_primary(tmp_path):
    ident = os_release.identify(_write(tmp_path, ARCH))
    assert ident.id == "arch"
    assert ident.known
    assert ident.is_primary
    assert ident.is_arch_derived
    assert "Arch Linux" in ident.label


def test_identify_derivative_splits_id_like(tmp_path):
    ident = os_release.identify(_write(tmp_path, DERIVATIVE))
    assert ident.id == "cachyos"
    assert ident.id_like == ("arch",)
    assert not ident.is_primary
    assert ident.is_arch_derived
    assert "ID_LIKE=arch" in ident.label


def test_identify_multi_parent_id_like(tmp_path):
    ident = os_release.identify(_write(tmp_path, "ID=someos\nID_LIKE='arch archarm'\n"))
    assert ident.id_like == ("arch", "archarm")
    assert ident.is_arch_derived


def test_identify_foreign(tmp_path):
    ident = os_release.identify(_write(tmp_path, FOREIGN))
    assert ident.id == "debian"
    assert not ident.is_arch_derived


def test_identify_lowercases_id(tmp_path):
    ident = os_release.identify(_write(tmp_path, "ID=Arch\nID_LIKE=Arch\n"))
    assert ident.is_primary
    assert ident.id_like == ("arch",)


def test_identify_unknown_when_absent(tmp_path):
    ident = os_release.identify((tmp_path / "nope",))
    assert not ident.known
    assert ident.source is None
    assert ident.id == "linux"          # os-release(5) default
    assert not ident.is_arch_derived


def test_identify_defaults_id_when_field_missing(tmp_path):
    ident = os_release.identify(_write(tmp_path, 'PRETTY_NAME="Mystery"\n'))
    assert ident.known                  # the file WAS read
    assert ident.id == "linux"


# --- findings ---------------------------------------------------------------

def test_primary_is_silent_by_default(tmp_path):
    """A plain Arch host learns nothing from being told it is Arch."""
    assert os_release.collect_distro_findings(paths=_write(tmp_path, ARCH)) == []


def test_primary_reports_identity_when_explicit(tmp_path):
    findings = os_release.collect_distro_findings(
        explicit=True, paths=_write(tmp_path, ARCH))
    assert [f.check_id for f in findings] == ["distro_primary"]
    assert findings[0].severity == diag.SEV_INFO
    assert findings[0].category == "distro"


def test_derivative_reports_identity_without_the_flag(tmp_path):
    findings = os_release.collect_distro_findings(paths=_write(tmp_path, DERIVATIVE))
    assert [f.check_id for f in findings] == ["distro_derivative"]
    assert findings[0].severity == diag.SEV_INFO
    assert "CachyOS" in findings[0].message
    # The support tier is the point of the line: name what is NOT validated.
    assert "kernel" in findings[0].remediation


def test_foreign_distro_warns(tmp_path):
    findings = os_release.collect_distro_findings(paths=_write(tmp_path, FOREIGN))
    assert [f.check_id for f in findings] == ["distro_unsupported"]
    assert findings[0].severity == diag.SEV_WARN


def test_missing_os_release_warns(tmp_path):
    findings = os_release.collect_distro_findings(paths=(tmp_path / "nope",))
    assert [f.check_id for f in findings] == ["distro_unknown"]
    assert findings[0].severity == diag.SEV_WARN


def test_no_finding_is_ever_an_error(tmp_path):
    """A support tier must not change doctor's exit code."""
    for text in (ARCH, DERIVATIVE, FOREIGN):
        findings = os_release.collect_distro_findings(
            explicit=True, paths=_write(tmp_path, text))
        assert diag.error_count(findings) == 0
    assert diag.error_count(
        os_release.collect_distro_findings(paths=(tmp_path / "nope",))) == 0
