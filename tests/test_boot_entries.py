"""Tests for primitives/boot_entries.py (3.3.0-F5 / 3.3.0-B10)."""
import hashlib
import shutil
import subprocess

import pytest

from sysforge.primitives import boot_entries as be
from sysforge.primitives.privilege import privileged_argv

OPERATOR = (
    "title Arch Linux Sysforge\n"
    "linux /vmlinuz-linux-sysforge\n"
    "initrd /amd-ucode.img\n"
    "initrd /initramfs-linux-sysforge.img\n"
    "options root=UUID=abc rw nvidia-drm.modeset=1\n"
    "options quiet\n"
)


@pytest.fixture(autouse=True)
def _microcode_on_esp():
    """OPERATOR's microcode initrd exists on the (conftest) fake ESP."""
    (be.BOOT_DIR / "amd-ucode.img").write_bytes(b"")


def test_parse_keeps_order_and_repeats():
    e = be.parse_entry("97-arch-custom.conf", OPERATOR)
    assert e.get("linux") == "/vmlinuz-linux-sysforge"
    assert e.get_all("initrd") == ["/amd-ucode.img", "/initramfs-linux-sysforge.img"]
    assert e.get_all("options") == ["root=UUID=abc rw nvidia-drm.modeset=1", "quiet"]
    assert e.linux_basename == "vmlinuz-linux-sysforge"
    assert e.header is None


def test_parse_tabs_comments_crlf_bom():
    text = "﻿# my entry\r\ntitle\tX\r\nlinux\t/arch/vmlinuz-linux\r\n\r\n"
    e = be.parse_entry("x.conf", text)
    assert e.header == "# my entry"
    assert e.get("title") == "X"
    assert e.get("linux") == "/arch/vmlinuz-linux"
    assert e.linux_basename == "vmlinuz-linux"


def test_managed_header_roundtrip():
    h = "# sysforge-managed (template: 97-arch-custom.conf, role: autofdo) — regenerated"
    assert be.parse_managed_header(h) == be.ManagedMeta("97-arch-custom.conf", "autofdo")
    assert be.parse_managed_header("# something else") is None
    assert be.parse_managed_header(None) is None


@pytest.mark.parametrize("name,ok", [
    ("sysforge-linux-sysforge-fdo.conf", True),
    ("97-sysforge-linux-sysforge.conf", True),
    ("97-arch-custom.conf", False),
    ("sysforge-x.conf.tmp", False),
])
def test_is_sysforge_name(name, ok):
    assert be.is_sysforge_name(name) is ok


def test_is_managed_needs_name_and_header():
    hdr = "# sysforge-managed (template: a.conf, role: plain) — x\n"
    assert be.is_managed(be.parse_entry("sysforge-k.conf", hdr + "linux /vmlinuz-k\n"))
    assert not be.is_managed(be.parse_entry("sysforge-k.conf", "linux /vmlinuz-k\n"))
    assert not be.is_managed(be.parse_entry("97-k.conf", hdr + "linux /vmlinuz-k\n"))


@pytest.mark.parametrize("prefix", ["97", "a_1"])
def test_validate_prefix_ok(prefix):
    be.validate_prefix(prefix)
    be.validate_prefix(None)


@pytest.mark.parametrize("prefix", ["", "9-7", "a/b", "a.b", " 97", "97\n"])
def test_validate_prefix_rejects(prefix):
    with pytest.raises(ValueError):
        be.validate_prefix(prefix)


def test_entry_filename():
    assert be.entry_filename("linux-sysforge-fdo", None) == "sysforge-linux-sysforge-fdo.conf"
    assert be.entry_filename("linux-sysforge-fdo", "97") == "97-sysforge-linux-sysforge-fdo.conf"


def test_title_for_roles():
    assert be.title_for("Arch Linux", "plain", "k") == "Arch Linux (sysforge)"
    assert be.title_for("Arch Linux", "profiling", "k") == (
        "Arch Linux (sysforge, profiling)"
    )
    assert be.title_for("Arch Linux", "autofdo", "k") == "Arch Linux (sysforge, AutoFDO)"
    assert be.title_for("Arch Linux", "propeller", "k") == (
        "Arch Linux (sysforge, AutoFDO + Propeller)"
    )
    assert be.title_for("Arch Linux", "weird", "linux-x") == (
        "Arch Linux (sysforge, linux-x)"
    )


def test_render_from_template_without_sort_key():
    tpl = be.parse_entry("97-arch-custom.conf", OPERATOR + "machine-id 0123\n")
    out = be.render_entry(template=tpl, template_kernel="linux-sysforge",
                          kernel="linux-sysforge-fdo", role="autofdo",
                          title="Arch Linux (sysforge, AutoFDO)", version="7.2.7")
    lines = out.splitlines()
    assert lines[0].startswith("# sysforge-managed (template: 97-arch-custom.conf, role: autofdo)")
    assert "title Arch Linux (sysforge, AutoFDO)" in lines
    assert "linux /vmlinuz-linux-sysforge-fdo" in lines
    assert "initrd /amd-ucode.img" in lines
    assert "initrd /initramfs-linux-sysforge-fdo.img" in lines
    assert "options root=UUID=abc rw nvidia-drm.modeset=1" in lines and "options quiet" in lines
    assert "machine-id 0123" in lines
    assert not any(
        line.startswith(("sort-key", "version")) for line in lines
    )  # no sort-key → no version


def test_render_with_sort_key_copies_and_sets_version():
    tpl = be.parse_entry("t.conf", OPERATOR + "sort-key arch\nversion 7.1.0\n")
    out = be.render_entry(template=tpl, template_kernel="linux-sysforge", kernel="k",
                          role="plain", title="T", version="7.2.7-arch1-1")
    assert "sort-key arch" in out.splitlines()
    assert "version 7.2.7-arch1-1" in out.splitlines()


def test_render_keeps_subdir_prefix():
    tpl = be.parse_entry("t.conf", "linux /arch/vmlinuz-linux\ninitrd /arch/initramfs-linux.img\n")
    out = be.render_entry(template=tpl, template_kernel="linux", kernel="k",
                          role="plain", title="T", version=None)
    assert "linux /arch/vmlinuz-k" in out.splitlines()
    assert "initrd /arch/initramfs-k.img" in out.splitlines()


def test_boots_kernel_exact():
    e = be.parse_entry("a.conf", "linux /vmlinuz-linux-sysforge-fdo\n")
    assert be.boots_kernel(e, "linux-sysforge-fdo")
    assert not be.boots_kernel(e, "linux-sysforge")


def _e(name, text):
    return be.parse_entry(name, text)


MANAGED_HDR = "# sysforge-managed (template: 97-arch-custom.conf, role: {role}) — r\n"


def test_read_selected_entry(tmp_path):
    p = tmp_path / "var"
    p.write_bytes(b"\x07\x00\x00\x00" + "97-arch-custom.conf\x00".encode("utf-16-le"))
    assert be.read_selected_entry(p) == "97-arch-custom.conf"
    assert be.read_selected_entry(tmp_path / "missing") is None


def test_read_pretty_name(tmp_path):
    p = tmp_path / "os-release"
    p.write_text('NAME="Arch Linux"\nPRETTY_NAME="Arch Linux"\n')
    assert be.read_pretty_name(p) == "Arch Linux"
    assert be.read_pretty_name(tmp_path / "nope") == "Linux"


def test_template_from_selected_operator_entry():
    ents = [_e("97-arch-custom.conf", OPERATOR), _e("99-arch.conf", "linux /vmlinuz-linux\n")]
    t = be.resolve_template(ents, selected="97-arch-custom.conf", pkgname="linux-sysforge")
    assert t.entry.filename == "97-arch-custom.conf" and t.kernel == "linux-sysforge"


def test_template_follows_managed_chain():
    managed = _e("sysforge-linux-sysforge-profiling.conf",
                 MANAGED_HDR.format(role="profiling") + "linux /vmlinuz-linux-sysforge-profiling\n")
    ents = [_e("97-arch-custom.conf", OPERATOR), managed]
    t = be.resolve_template(ents, selected=managed.filename, pkgname="linux-sysforge")
    assert t.entry.filename == "97-arch-custom.conf"


def test_template_managed_chain_broken_uses_managed_itself():
    managed = _e(
        "sysforge-k.conf",
        MANAGED_HDR.format(role="plain") + "linux /vmlinuz-k\noptions x\n",
    )
    t = be.resolve_template([managed], selected="sysforge-k.conf", pkgname="linux-sysforge")
    assert t.entry.filename == "sysforge-k.conf" and t.kernel == "k"


def test_template_selected_missing_falls_back_to_pkgname_then_linux():
    ents = [_e("99-arch.conf", "linux /vmlinuz-linux\n"), _e("97-arch-custom.conf", OPERATOR)]
    t1 = be.resolve_template(ents, selected="gone.conf", pkgname="linux-sysforge")
    assert t1.entry.filename == "97-arch-custom.conf"
    t2 = be.resolve_template(ents, selected=None, pkgname="linux-mine")
    assert t2.entry.filename == "99-arch.conf"


def test_template_skips_efi_and_auto():
    ents = [_e("uki.conf", "efi /EFI/Linux/arch.efi\n")]
    with pytest.raises(be.BootEntryError):
        be.resolve_template(ents, selected="uki.conf", pkgname="linux")
    with pytest.raises(be.BootEntryError):
        be.resolve_template([], selected="auto-windows", pkgname="linux")


def _plan(ents, wanted, images, prefix=None, tpl_text=OPERATOR):
    tpl = be.Template(be.parse_entry("97-arch-custom.conf", tpl_text), "linux-sysforge")
    return be.plan_sync(ents, template=tpl, wanted=wanted, existing_images=images,
                        prefix=prefix, pretty_name="Arch Linux")


def test_plan_writes_new_kernel():
    p = _plan([_e("97-arch-custom.conf", OPERATOR)],
              [be.WantedKernel("linux-sysforge-fdo", "autofdo", "7.2.7")],
              {"linux-sysforge", "linux-sysforge-fdo"})
    assert [w[0] for w in p.writes] == ["sysforge-linux-sysforge-fdo.conf"]
    assert p.deletes == ()


def test_plan_operator_coverage_writes_nothing_and_drops_our_duplicate():
    ours = _e(
        "sysforge-linux-sysforge.conf",
        MANAGED_HDR.format(role="plain") + "linux /vmlinuz-linux-sysforge\n",
    )
    p = _plan([_e("97-arch-custom.conf", OPERATOR), ours],
              [be.WantedKernel("linux-sysforge", "plain", None)], {"linux-sysforge"})
    assert p.writes == ()
    assert p.deletes == ("sysforge-linux-sysforge.conf",)
    assert p.notes  # ui line explaining the removal


def test_plan_rerenders_all_managed_and_prunes_missing():
    hdr_prop = MANAGED_HDR.format(role="propeller")
    text_prop = hdr_prop + "linux /vmlinuz-linux-sysforge-propeller\nversion 7.2.6\n"
    a = _e("sysforge-linux-sysforge-propeller.conf", text_prop)
    hdr_prof = MANAGED_HDR.format(role="profiling")
    text_prof = hdr_prof + "linux /vmlinuz-linux-sysforge-profiling\n"
    gone = _e("sysforge-linux-sysforge-profiling.conf", text_prof)
    p = _plan(
        [_e("97-arch-custom.conf", OPERATOR), a, gone],
        [be.WantedKernel("linux-sysforge-fdo", "autofdo", "7.2.7")],
        {"linux-sysforge", "linux-sysforge-fdo", "linux-sysforge-propeller"},
    )
    names = sorted(w[0] for w in p.writes)
    assert names == [
        "sysforge-linux-sysforge-fdo.conf",
        "sysforge-linux-sysforge-propeller.conf",
    ]
    assert p.deletes == ("sysforge-linux-sysforge-profiling.conf",)
    prop = dict(p.writes)["sysforge-linux-sysforge-propeller.conf"]
    assert "(sysforge, AutoFDO + Propeller)" in prop


def test_plan_prefix_change_renames_managed():
    hdr_fdo = MANAGED_HDR.format(role="autofdo")
    text_fdo = hdr_fdo + "linux /vmlinuz-linux-sysforge-fdo\n"
    old = _e("sysforge-linux-sysforge-fdo.conf", text_fdo)
    p = _plan(
        [_e("97-arch-custom.conf", OPERATOR), old],
        [],
        {"linux-sysforge-fdo", "linux-sysforge"},
        prefix="97",
    )
    assert [w[0] for w in p.writes] == ["97-sysforge-linux-sysforge-fdo.conf"]
    assert p.deletes == ("sysforge-linux-sysforge-fdo.conf",)


def test_plan_prefix_ignored_with_sort_key():
    p = _plan([], [be.WantedKernel("k", "plain", "7.2.7")], {"k"}, prefix="97",
              tpl_text=OPERATOR + "sort-key arch\n")
    assert [w[0] for w in p.writes] == ["sysforge-k.conf"]
    assert any("sort-key" in line for line in p.info)


def test_plan_never_touches_headerless_sysforge_named_file():
    mine = _e("sysforge-k.conf", "linux /vmlinuz-k\n")  # operator-owned (no header)
    p = _plan([mine], [be.WantedKernel("k", "plain", None)], {"k"})
    assert p.writes == () and p.deletes == ()  # it counts as coverage


def test_plan_prune_only():
    gone = _e("sysforge-x.conf", MANAGED_HDR.format(role="plain") + "linux /vmlinuz-x\n")
    keep = _e("sysforge-y.conf", MANAGED_HDR.format(role="plain") + "linux /vmlinuz-y\n")
    op = _e("10-x.conf", "linux /vmlinuz-x\n")  # operator dangling: never pruned
    p = be.plan_prune([gone, keep, op], {"y"})
    assert p.deletes == ("sysforge-x.conf",) and p.writes == ()


def test_rendered_entry_roundtrips_as_managed():
    tpl = be.parse_entry("97-arch-custom.conf", OPERATOR)
    rendered = be.render_entry(template=tpl, template_kernel="linux-sysforge",
                               kernel="k", role="autofdo",
                               title="Arch Linux (sysforge, AutoFDO)", version=None)
    entry = be.parse_entry("sysforge-k.conf", rendered)
    assert be.is_managed(entry)
    assert be.parse_managed_header(entry.header) == be.ManagedMeta("97-arch-custom.conf", "autofdo")


def _boot(tmp_path):
    entries = tmp_path / "loader" / "entries"
    entries.mkdir(parents=True)
    return tmp_path, entries


def _digest(entries):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in entries.iterdir() if not be.is_sysforge_name(p.name)}


def _fake_run_ok(calls):
    prefix = privileged_argv([])

    def run(argv, **kw):
        calls.append(argv)
        # emulate cp / mv / rm without escalation for the fixture tree
        cmd = list(argv[len(prefix):]) if list(argv[:len(prefix)]) == prefix else list(argv)
        if cmd[0] == "cp":
            subprocess.run(["cp", cmd[1], cmd[2]], check=True)
        elif cmd[0] == "mv":
            subprocess.run(["mv", cmd[1], cmd[2]], check=True)
        elif cmd[0] == "rm":
            subprocess.run(["rm", "-f", cmd[-1]], check=True)
        return subprocess.CompletedProcess(argv, 0, "", "")
    return run


def test_existing_images(tmp_path):
    (tmp_path / "vmlinuz-linux").write_bytes(b"x")
    (tmp_path / "vmlinuz-linux-sysforge").write_bytes(b"x")
    (tmp_path / "initramfs-linux.img").write_bytes(b"x")
    assert be.existing_images(tmp_path) == {"linux", "linux-sysforge"}


def test_check_boot_path(monkeypatch):
    monkeypatch.setattr(be, "BOOT_DIR", be.Path("/boot"))  # fixture fakes it; probe is injected
    def ok(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "/boot\n", "")

    be.check_boot_path(run=ok)
    def efi(argv, **kw):
        return subprocess.CompletedProcess(argv, 0, "/efi\n", "")

    with pytest.raises(be.BootEntryError, match='boot_entries = "off"'):
        be.check_boot_path(run=efi)
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "no ESP")

    with pytest.raises(be.BootEntryError):
        be.check_boot_path(run=fail)


def test_apply_writes_deletes_and_preserves_operator_files(tmp_path, monkeypatch):
    boot, entries = _boot(tmp_path)
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    (entries / "97-arch-custom.conf").write_text(OPERATOR)
    (entries / "sysforge-k.conf").write_text("linux /vmlinuz-k\n")  # operator-owned name
    stale = entries / "sysforge-old.conf"
    stale.write_text(MANAGED_HDR.format(role="plain") + "linux /vmlinuz-old\n")
    before = _digest(entries)
    before_hdrless = (entries / "sysforge-k.conf").read_bytes()
    calls = []
    plan = be.SyncPlan(writes=(("sysforge-new.conf", "linux /vmlinuz-new\n"),),
                       deletes=("sysforge-old.conf",))
    be.apply_plan(plan, dry_run=False, run=_fake_run_ok(calls))
    assert (entries / "sysforge-new.conf").read_text() == "linux /vmlinuz-new\n"
    assert not stale.exists()
    assert not list(entries.glob("*.tmp"))
    assert _digest(entries) == before
    assert (entries / "sysforge-k.conf").read_bytes() == before_hdrless
    prefix = privileged_argv([])
    assert calls and all(c[:len(prefix)] == prefix for c in calls)


def test_apply_refuses_to_delete_unmanaged(tmp_path, monkeypatch):
    boot, entries = _boot(tmp_path)
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    (entries / "sysforge-k.conf").write_text("linux /vmlinuz-k\n")
    with pytest.raises(RuntimeError, match="not sysforge-managed"):
        be.apply_plan(be.SyncPlan(deletes=("sysforge-k.conf",)), dry_run=False,
                      run=_fake_run_ok([]))
    assert (entries / "sysforge-k.conf").exists()


def test_apply_dry_run_touches_nothing(tmp_path, monkeypatch):
    boot, entries = _boot(tmp_path)
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    calls = []
    lines = be.apply_plan(be.SyncPlan(writes=(("sysforge-a.conf", "linux /vmlinuz-a\n"),)),
                          dry_run=True, run=_fake_run_ok(calls))
    assert calls == [] and not (entries / "sysforge-a.conf").exists()
    assert lines[0] == "[dry-run] would write sysforge-a.conf:"


def test_apply_privileged_failure_names_file(tmp_path, monkeypatch):
    boot, entries = _boot(tmp_path)
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    (entries / "97-arch-custom.conf").write_text(OPERATOR)
    before = _digest(entries)
    def deny(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "sudo: denied")

    with pytest.raises(RuntimeError, match="sysforge-a.conf"):
        be.apply_plan(be.SyncPlan(writes=(("sysforge-a.conf", "x\n"),)), dry_run=False, run=deny)
    assert _digest(entries) == before


def test_load_entries_unreadable_dir(tmp_path):
    with pytest.raises(OSError):
        be.load_entries(tmp_path / "missing")


def test_isolation_fixture_fake_esp_is_default():
    be.check_boot_path()
    assert be.read_selected_entry() is None
    assert [e.filename for e in be.load_entries(be.entries_dir())] == ["99-test.conf"]


def test_isolation_guard_blocks_outside_writes():
    dst = "/etc/sysforge-test-should-not-exist"
    with pytest.raises(AssertionError):
        be._run(["cp", "/etc/hostname", dst])
    import os
    assert not os.path.exists(dst)


def test_apply_plan_default_runner_writes_into_fake_esp():
    keep = be.entries_dir() / "99-test.conf"
    before = keep.read_bytes()
    be.apply_plan(be.SyncPlan(writes=(("sysforge-x.conf", "linux /vmlinuz-x\n"),)),
                  dry_run=False)
    assert (be.entries_dir() / "sysforge-x.conf").read_text() == "linux /vmlinuz-x\n"
    assert keep.read_bytes() == before


def test_plan_skips_write_onto_operator_named_file():
    mine = _e("sysforge-linux.conf", "linux /vmlinuz-other\n")
    p = _plan([_e("97-arch-custom.conf", OPERATOR), mine],
              [be.WantedKernel("linux", "plain", None)], {"linux", "other"})
    assert all(w[0] != "sysforge-linux.conf" for w in p.writes)
    assert p.deletes == ()
    assert any("cannot write sysforge-linux.conf" in n for n in p.notes)


def test_apply_refuses_to_overwrite_unmanaged(tmp_path, monkeypatch):
    boot, entries = _boot(tmp_path)
    monkeypatch.setattr(be, "BOOT_DIR", boot)
    f = entries / "sysforge-k.conf"
    f.write_text("linux /vmlinuz-k\n")
    before = f.read_bytes()
    calls = []
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        be.apply_plan(be.SyncPlan(writes=(("sysforge-k.conf", "x\n"),)), dry_run=False,
                      run=_fake_run_ok(calls))
    assert f.read_bytes() == before and calls == []


def test_effective_prefix_both_branches():
    plain = be.Template(be.parse_entry("a.conf", "title A\nlinux /vmlinuz-linux\n"), "linux")
    keyed = be.Template(
        be.parse_entry("a.conf", "title A\nsort-key arch\nlinux /vmlinuz-linux\n"), "linux")
    assert be.effective_prefix(plain, "p") == "p"
    assert be.effective_prefix(plain, None) is None
    assert be.effective_prefix(keyed, "p") is None


def test_audit_findings():
    hdr_ok = "# sysforge-managed (template: 97-arch-custom.conf, role: autofdo) — r\n"
    hdr_gone = "# sysforge-managed (template: deleted.conf, role: plain) — r\n"
    ents = [
        be.parse_entry("97-arch-custom.conf", "linux /vmlinuz-linux-sysforge\n"),
        be.parse_entry("sysforge-a.conf", hdr_ok + "linux /vmlinuz-a\n"),      # image missing
        be.parse_entry("sysforge-b.conf", hdr_gone + "linux /vmlinuz-b\n"),    # template gone
    ]
    ids = sorted(f[0] for f in be.audit(ents, {"linux-sysforge", "b", "c"}))
    assert ids == ["boot_entry_dangling", "boot_entry_missing", "boot_entry_template_gone"]


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
@pytest.mark.parametrize(
    "hi,lo",
    [
        ("96-memtest", "sysforge-x"),
        ("97-sysforge-x", "97-arch-custom"),
        ("98-a", "97-sysforge-x"),
    ],
)
def test_spec_ordering_claims_hold(hi, lo):
    r = subprocess.run(
        ["systemd-analyze", "compare-versions", hi, lo], capture_output=True, text=True
    )
    assert f"{hi} > {lo}" in r.stdout



# ---------------------------------------------------------------------------
# R13: fallback swap + rendered-path validation; R15: header comma
# ---------------------------------------------------------------------------

def test_render_swaps_fallback_initramfs_to_new_main_initramfs():
    tpl = be.parse_entry(
        "97-fallback.conf",
        "linux /vmlinuz-linux\ninitrd /amd-ucode.img\ninitrd /initramfs-linux-fallback.img\n",
    )
    out = be.render_entry(template=tpl, template_kernel="linux", kernel="k",
                          role="plain", title="T", version=None).splitlines()
    assert "initrd /initramfs-k.img" in out
    assert "initrd /initramfs-linux-fallback.img" not in out
    assert "initrd /amd-ucode.img" in out


def test_template_name_with_comma_roundtrips():
    tpl = be.parse_entry("a,b.conf", OPERATOR)
    rendered = be.render_entry(template=tpl, template_kernel="linux-sysforge", kernel="k",
                               role="plain", title="T", version=None)
    entry = be.parse_entry("sysforge-k.conf", rendered)
    assert be.parse_managed_header(entry.header) == be.ManagedMeta("a,b.conf", "plain")


def _rendered(tpl_text, template_kernel, kernel):
    tpl = be.parse_entry("t.conf", tpl_text)
    return be.render_entry(template=tpl, template_kernel=template_kernel, kernel=kernel,
                           role="plain", title="T", version=None)


def test_missing_references_all_present():
    (be.BOOT_DIR / "vmlinuz-k").write_bytes(b"")
    (be.BOOT_DIR / "initramfs-k.img").write_bytes(b"")
    r = _rendered("linux /vmlinuz-linux\ninitrd /initramfs-linux.img\n", "linux", "k")
    assert be.missing_references(r, "k", {"k"}) == []


def test_missing_references_microcode_present():
    (be.BOOT_DIR / "vmlinuz-k").write_bytes(b"")
    (be.BOOT_DIR / "initramfs-k.img").write_bytes(b"")
    r = _rendered(OPERATOR, "linux-sysforge", "k")
    assert be.missing_references(r, "k", {"k"}) == []


def test_missing_references_booster_initrd_missing():
    (be.BOOT_DIR / "vmlinuz-k").write_bytes(b"")
    r = _rendered("linux /vmlinuz-linux\ninitrd /booster-linux.img\n", "linux", "k")
    assert be.missing_references(r, "k", {"k"}) == ["/booster-linux.img"]


def test_missing_references_subdir_layout():
    (be.BOOT_DIR / "vmlinuz-k").write_bytes(b"")
    (be.BOOT_DIR / "EFI" / "arch").mkdir(parents=True)
    r = _rendered("linux /EFI/arch/vmlinuz-linux\ninitrd /EFI/arch/initramfs-linux.img\n",
                  "linux", "k")
    assert be.missing_references(r, "k", {"k"}) == [
        "/EFI/arch/vmlinuz-k", "/EFI/arch/initramfs-k.img"]


def test_missing_references_dry_run_own_root_files_valid():
    r = _rendered("linux /vmlinuz-linux\ninitrd /initramfs-linux.img\n", "linux", "newk")
    assert not (be.BOOT_DIR / "vmlinuz-newk").exists()
    assert be.missing_references(r, "newk", {"newk"}) == []
    assert be.missing_references(r, "newk", set()) == [
        "/vmlinuz-newk", "/initramfs-newk.img"]


def test_plan_skips_subdir_template_with_note():
    tpl_text = ("linux /EFI/arch/vmlinuz-linux-sysforge\n"
                "initrd /EFI/arch/initramfs-linux-sysforge.img\n")
    p = _plan([], [be.WantedKernel("k", "plain", None)], {"k"}, tpl_text=tpl_text)
    assert p.writes == () and p.skipped == ("k",)
    assert any(n.startswith("cannot write sysforge-k.conf: it would reference "
                            "/EFI/arch/vmlinuz-k, /EFI/arch/initramfs-k.img")
               and 'boot_entries = "off"' in n for n in p.notes)


def test_plan_skips_missing_initrd_and_keeps_existing_entry():
    old = _e("sysforge-k.conf", MANAGED_HDR.format(role="plain") + "linux /vmlinuz-k\n")
    tpl_text = "linux /vmlinuz-linux-sysforge\ninitrd /booster-linux-sysforge.img\n"
    p = _plan([old], [be.WantedKernel("k", "plain", None)], {"k"}, tpl_text=tpl_text)
    assert p.writes == () and p.deletes == () and p.skipped == ("k",)
    assert any("/booster-linux-sysforge.img" in n for n in p.notes)


def test_plan_name_collision_populates_skipped():
    mine = _e("sysforge-k.conf", "linux /vmlinuz-other\n")  # operator-owned, other image
    p = _plan([mine], [be.WantedKernel("k", "plain", None)], {"k"})
    assert p.writes == () and p.skipped == ("k",)
