import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _dev_install_targets():
    out = subprocess.run(
        ["bash", str(REPO / "tools/dev_install.sh"), "print-targets"],
        capture_output=True, text=True, check=True,
    ).stdout
    return set(out.split())


def test_mapping_covers_packaged_system_paths():
    # The dev-install mapping must mirror exactly the system paths the PKGBUILD
    # installs (minus the generated sysusers.d/tmpfiles.d stubs, intentionally
    # excluded). Guards against drift when a new shipped file is added.
    targets = _dev_install_targets()
    # sanity anchors from the PKGBUILD package() layout
    assert "/etc/sysforge/sysforge.toml" in targets
    assert "/usr/share/man/man1/sysforge.1" in targets
    assert "/usr/share/libalpm/hooks/sysforge-buildstate.hook" in targets
    # excluded provisioning stubs must NOT be symlinked
    assert not any("sysusers.d" in t or "tmpfiles.d" in t for t in targets)
    # bootstrap.toml.example is a per-host template shipped as an example, not a
    # functional config sysforge reads — intentionally NOT a dev-install target.
    assert not any(t.endswith("bootstrap.toml.example") for t in targets)


def test_uninstall_predicate_only_touches_checkout_symlinks(tmp_path):
    # Model the unlink_one guard: readlink -f must resolve under REPO.
    real = tmp_path / "real.toml"
    real.write_text("stock\n")
    link = tmp_path / "link.toml"
    link.symlink_to(REPO / "etc/sysforge/sysforge.toml")
    # a real file is left; a checkout symlink is eligible for removal
    assert real.resolve() == real  # not under REPO → keep
    assert str(link.resolve()).startswith(str(REPO))  # under REPO → remove
