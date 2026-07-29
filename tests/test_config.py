"""
test_config.py — tests for config.py: load_config, load_conflict_groups,
load_consumes_inference, _parse_one_makepkg_conf, and find_pkgbuild.
"""
import pytest

from sysforge.primitives.config import (
    _parse_one_makepkg_conf,
    load_config,
    load_conflict_groups,
    load_consumes_inference,
    normalize_package_entry,
    pgo_warns_for,
    resolve_drift_detect,
    resolve_pgo_allowlist,
    resolve_repo_mode,
)


class _WarnRecorder:
    """Records warn() messages, standing in for a module logger (mirrors the
    throttle tests' recorder) so enum-fallback warnings can be asserted."""

    def __init__(self):
        self.messages: list[str] = []

    def warn(self, msg):
        self.messages.append(msg)


# ---------------------------------------------------------------------------
# resolve_drift_detect (Q9 — [toolchain] drift_detect)
# ---------------------------------------------------------------------------

def test_drift_detect_defaults_to_fingerprint_when_file_absent(tmp_path):
    assert resolve_drift_detect(tmp_path / "nope.toml") == "fingerprint"


def test_drift_detect_defaults_to_fingerprint_when_key_absent(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text("enabled = false\n")
    assert resolve_drift_detect(p) == "fingerprint"


def test_drift_detect_reads_content_hash(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text('drift_detect = "content_hash"\n')
    assert resolve_drift_detect(p) == "content_hash"


def test_drift_detect_unknown_value_falls_back_to_fingerprint(tmp_path):
    p = tmp_path / "toolchain.toml"
    p.write_text('drift_detect = "bogus"\n')
    assert resolve_drift_detect(p) == "fingerprint"


# ---------------------------------------------------------------------------
# resolve_repo_mode
# ---------------------------------------------------------------------------

class TestResolveRepoMode:
    def test_default_is_pacman(self):
        assert resolve_repo_mode({}) == "pacman"
        assert resolve_repo_mode(None) == "pacman"

    def test_explicit_build_from_source(self):
        assert resolve_repo_mode({"repo_mode": "build_from_source"}) == "build_from_source"

    def test_profiled_is_rejected_not_silently_downgraded(self):
        """3.0.0 removed the alias. It must fail loudly, never resolve to pacman.

        Regression guard for the whitelist hazard: if "profiled" is still in
        REPO_MODE_ACCEPTED_INPUTS but no longer mapped, _load_packages accepts
        it and resolve_enum silently yields "pacman" — turning off the user's
        source builds with no error at all.
        """
        from sysforge.primitives.config import REPO_MODE_ACCEPTED_INPUTS

        assert "profiled" not in REPO_MODE_ACCEPTED_INPUTS

    def test_profiled_raises_from_resolve_repo_mode_directly(self):
        """The reject must live in resolve_repo_mode itself, not only at the
        _load_packages whitelist gate — five of six callers (build_cmd,
        reconfigure x2, update_assemble, toolchain, llvm_state) read
        packages.toml directly and call resolve_repo_mode without ever passing
        through that gate. If only the gate rejected "profiled", those callers
        would still silently downgrade to "pacman"."""
        with pytest.raises(ValueError, match="build_from_source"):
            resolve_repo_mode({"repo_mode": "profiled"})

    def test_unknown_value_warns_and_falls_back_to_pacman(self, monkeypatch):
        # 2.3.0-F8: unknowns no longer pass through unvalidated. resolve_enum
        # validates against the known vocabulary, warns, and falls back to the
        # documented default (pacman).
        import sysforge.primitives.config as cfg

        rec = _WarnRecorder()
        monkeypatch.setattr(cfg, "_log", rec)
        assert resolve_repo_mode({"repo_mode": "hybrid"}) == "pacman"
        assert any("hybrid" in m and "repo_mode" in m for m in rec.messages)

    def test_typo_value_still_warns_and_falls_back_not_strict(self, monkeypatch):
        """Pin that the reject is narrow: an ordinary typo (not the removed
        "profiled" token) still takes the lenient warn-and-default path,
        proving resolve_repo_mode was not made strict in general."""
        import sysforge.primitives.config as cfg

        rec = _WarnRecorder()
        monkeypatch.setattr(cfg, "_log", rec)
        assert resolve_repo_mode({"repo_mode": "pacmn"}) == "pacman"
        assert any("pacmn" in m and "repo_mode" in m for m in rec.messages)


# ---------------------------------------------------------------------------
# resolve_pgo_allowlist / pgo_warns_for (F5: --pgo warn-list, seeded with mesa)
# ---------------------------------------------------------------------------

class TestPgoAllowlist:
    def test_default_seeds_mesa(self):
        # No [pgo] section ⇒ mesa is still allow-listed (the supported case).
        assert "mesa" in resolve_pgo_allowlist({})
        assert "mesa" in resolve_pgo_allowlist(None)

    def test_explicit_allow_list_used(self):
        cfg = {"pgo": {"allow": ["mesa", "llvm"]}}
        allow = resolve_pgo_allowlist(cfg)
        assert {"mesa", "llvm"} <= allow

    def test_mesa_family_never_warns(self):
        # mesa-family is implicitly allowed regardless of config contents.
        cfg = {"pgo": {"allow": ["llvm"]}}
        assert not pgo_warns_for("mesa", cfg)
        assert not pgo_warns_for("lib32-mesa", cfg)

    def test_allow_listed_package_does_not_warn(self):
        assert not pgo_warns_for("llvm", {"pgo": {"allow": ["llvm"]}})

    def test_unlisted_package_warns(self):
        assert pgo_warns_for("ffmpeg", {"pgo": {"allow": ["llvm"]}})
        assert pgo_warns_for("ffmpeg", {})


class TestNormalizePackageEntry:
    def test_legacy_key_is_no_longer_renamed_on_read(self):
        """3.0.0 removed the read-side rename; the stale key passes through."""
        assert normalize_package_entry({"name": "x", "pkgbuild_patch": True}) == {
            "name": "x", "pkgbuild_patch": True,
        }

    def test_current_key_passes_through(self):
        assert normalize_package_entry({"name": "x", "cache": False}) == {
            "name": "x", "cache": False,
        }


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

def _write_toml(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestLoadConfig:

    def test_system_only(self, tmp_path):
        sys = tmp_path / "system" / "profiles.toml"
        _write_toml(sys, """
[defaults]
profile = "standard"

[[rules]]
priority = 10
name = "base"
""")
        config = load_config(config_paths=[tmp_path / "missing.toml", sys])
        assert config["defaults"]["profile"] == "standard"
        assert config["rules"][0]["priority"] == 10

    def test_user_only(self, tmp_path):
        user = tmp_path / "user" / "profiles.toml"
        _write_toml(user, """
[defaults]
profile = "optimized"

[[rules]]
priority = 5
name = "opt"
""")
        config = load_config(config_paths=[user, tmp_path / "missing.toml"])
        assert config["defaults"]["profile"] == "optimized"

    def test_no_config_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="No profiles.toml found"):
            load_config(config_paths=[tmp_path / "a.toml", tmp_path / "b.toml"])

    def test_user_overrides_system_without_extends(self, tmp_path):
        sys = tmp_path / "system.toml"
        user = tmp_path / "user.toml"
        _write_toml(sys, """
[defaults]
profile = "standard"

[[rules]]
priority = 10
name = "sys"
""")
        _write_toml(user, """
[defaults]
profile = "optimized"

[[rules]]
priority = 5
name = "user"
""")
        config = load_config(config_paths=[user, sys])
        assert config["defaults"]["profile"] == "optimized"
        assert len(config["rules"]) == 1
        assert config["rules"][0]["name"] == "user"

    def test_extends_system_merges(self, tmp_path):
        sys = tmp_path / "system.toml"
        user = tmp_path / "user.toml"
        _write_toml(sys, """
[defaults]
profile = "standard"

[paths]
pkgbuild_src_dir = "/sys/src"

[[rules]]
priority = 10
name = "sys_rule"
""")
        _write_toml(user, """
extends_system = true

[defaults]
profile = "optimized"

[[rules]]
priority = 5
name = "user_rule"
""")
        config = load_config(config_paths=[user, sys])
        # User default overrides system default
        assert config["defaults"]["profile"] == "optimized"
        # System paths preserved (deep-merged)
        assert config["paths"]["pkgbuild_src_dir"] == "/sys/src"
        # Both rule sets present
        assert len(config["rules"]) == 2
        # System rule first, user rule second
        assert config["rules"][0]["name"] == "sys_rule"
        assert config["rules"][0]["priority"] == 10
        # User rule bumped by 100
        assert config["rules"][1]["name"] == "user_rule"
        assert config["rules"][1]["priority"] == 105

    def test_priority_out_of_range_raises(self, tmp_path):
        f = tmp_path / "bad.toml"
        _write_toml(f, """
[[rules]]
priority = 100
name = "too_high"
""")
        with pytest.raises(ValueError, match="invalid priority"):
            load_config(config_paths=[f])

    def test_missing_priority_raises(self, tmp_path):
        f = tmp_path / "bad.toml"
        _write_toml(f, """
[[rules]]
name = "no_priority"
""")
        with pytest.raises(ValueError, match="missing required 'priority'"):
            load_config(config_paths=[f])


# ---------------------------------------------------------------------------
# load_conflict_groups
# ---------------------------------------------------------------------------

class TestLoadConflictGroups:

    def test_no_files_returns_empty(self, tmp_path):
        result = load_conflict_groups([tmp_path / "a.toml", tmp_path / "b.toml"])
        assert result == {}

    def test_system_only(self, tmp_path):
        sys = tmp_path / "system.toml"
        _write_toml(sys, """
[append_conflict_groups]
pic = ["-fPIC", "-fpic"]
""")
        result = load_conflict_groups([tmp_path / "missing.toml", sys])
        assert result == {"pic": ["-fPIC", "-fpic"]}

    def test_user_overrides_without_extends(self, tmp_path):
        sys = tmp_path / "sys.toml"
        user = tmp_path / "user.toml"
        _write_toml(sys, """
[append_conflict_groups]
pic = ["-fPIC", "-fpic"]
""")
        _write_toml(user, """
[append_conflict_groups]
lto = ["-flto", "-fno-lto"]
""")
        result = load_conflict_groups([user, sys])
        assert "pic" not in result
        assert result == {"lto": ["-flto", "-fno-lto"]}

    def test_extends_system_merges(self, tmp_path):
        sys = tmp_path / "sys.toml"
        user = tmp_path / "user.toml"
        _write_toml(sys, """
[append_conflict_groups]
pic = ["-fPIC", "-fpic"]
lto = ["-flto", "-fno-lto"]
""")
        _write_toml(user, """
extends_system = true

[append_conflict_groups]
lto = ["-flto=thin", "-fno-lto"]
stack = ["-fstack-protector"]
""")
        result = load_conflict_groups([user, sys])
        # System pic preserved
        assert result["pic"] == ["-fPIC", "-fpic"]
        # User lto overrides system lto
        assert result["lto"] == ["-flto=thin", "-fno-lto"]
        # User stack added
        assert result["stack"] == ["-fstack-protector"]


# ---------------------------------------------------------------------------
# load_consumes_inference
# ---------------------------------------------------------------------------

class TestLoadConsumesInference:

    def test_no_files_returns_defaults(self, tmp_path):
        result = load_consumes_inference([tmp_path / "a.toml", tmp_path / "b.toml"])
        assert "cargo" in result
        assert "cmake" in result
        assert result["git"] == ["makepkg"]

    def test_system_only(self, tmp_path):
        sys = tmp_path / "sys.toml"
        _write_toml(sys, """
[consumes_inference]
cargo = ["makepkg", "rust"]
""")
        result = load_consumes_inference([tmp_path / "missing.toml", sys])
        assert result == {"cargo": ["makepkg", "rust"]}

    def test_extends_system_merges(self, tmp_path):
        sys = tmp_path / "sys.toml"
        user = tmp_path / "user.toml"
        _write_toml(sys, """
[consumes_inference]
cargo = ["makepkg", "rust"]
make  = ["makepkg"]
""")
        _write_toml(user, """
extends_system = true

[consumes_inference]
cargo = ["makepkg", "rust", "env"]
zig   = ["makepkg", "env"]
""")
        result = load_consumes_inference([user, sys])
        assert result["cargo"] == ["makepkg", "rust", "env"]  # user wins
        assert result["make"] == ["makepkg"]                   # system preserved
        assert result["zig"] == ["makepkg", "env"]             # user added


# ---------------------------------------------------------------------------
# _parse_one_makepkg_conf
# ---------------------------------------------------------------------------

class TestParseOneMakepkgConf:

    def test_simple_assignment(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        conf.write_text('CFLAGS="-O2 -pipe"\nMAKEFLAGS="-j8"\n')
        result = _parse_one_makepkg_conf(conf)
        assert result["CFLAGS"] == '"-O2 -pipe"'
        assert result["MAKEFLAGS"] == '"-j8"'

    def test_missing_file(self, tmp_path):
        result = _parse_one_makepkg_conf(tmp_path / "nonexistent")
        assert result == {}

    def test_export_prefix(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        conf.write_text('export CFLAGS="-O2"\n')
        result = _parse_one_makepkg_conf(conf)
        assert result["CFLAGS"] == '"-O2"'

    def test_backslash_continuation(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        # Real makepkg.conf uses backslash-newline for continuation.
        # Build the text with explicit join to avoid escaping issues.
        BS = chr(92)  # backslash
        text = f'LDFLAGS="-Wl,-O1 {BS}\n  --as-needed {BS}\n  -z,relro"\n'
        conf.write_text(text)
        result = _parse_one_makepkg_conf(conf)
        assert "--as-needed" in result["LDFLAGS"]
        assert "-z,relro" in result["LDFLAGS"]

    def test_multiline_array(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        conf.write_text('OPTIONS=(strip\n  docs\n  !libtool\n  !staticlibs)\n')
        result = _parse_one_makepkg_conf(conf)
        assert "strip" in result["OPTIONS"]
        assert "!libtool" in result["OPTIONS"]
        assert "!staticlibs" in result["OPTIONS"]

    def test_comments_and_blank_lines_skipped(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        conf.write_text('# comment\n\nCFLAGS="-O2"\n# another\n')
        result = _parse_one_makepkg_conf(conf)
        assert len(result) == 1
        assert result["CFLAGS"] == '"-O2"'

    def test_unreadable_file(self, tmp_path):
        conf = tmp_path / "makepkg.conf"
        conf.write_text("CFLAGS=\"-O2\"")
        conf.chmod(0o000)
        result = _parse_one_makepkg_conf(conf)
        # Restoring permissions for cleanup
        conf.chmod(0o644)
        assert result == {}


# ---------------------------------------------------------------------------
# find_pkgbuild
# ---------------------------------------------------------------------------

class TestFindPkgbuild:

    def test_direct_directory(self, tmp_path):
        from sysforge.primitives.config import find_pkgbuild
        pkg_dir = tmp_path / "htop"
        pkg_dir.mkdir()
        pkgbuild = pkg_dir / "PKGBUILD"
        pkgbuild.write_text("pkgname=htop\n")

        result = find_pkgbuild(str(pkg_dir))
        assert result == pkgbuild.resolve()

    def test_direct_file(self, tmp_path):
        from sysforge.primitives.config import find_pkgbuild
        pkgbuild = tmp_path / "PKGBUILD"
        pkgbuild.write_text("pkgname=foo\n")

        result = find_pkgbuild(str(pkgbuild))
        assert result == pkgbuild.resolve()

    def test_not_found_raises(self, tmp_path, monkeypatch):
        from sysforge.primitives.config import find_pkgbuild
        monkeypatch.chdir(tmp_path)
        with pytest.raises(FileNotFoundError, match="PKGBUILD not found"):
            find_pkgbuild("nonexistent_package_xyz")

    def test_pkgbuild_src_dir_fallback(self, tmp_path):
        from sysforge.primitives.config import find_pkgbuild
        src_dir = tmp_path / "sources"
        pkg_dir = src_dir / "mypkg"
        pkg_dir.mkdir(parents=True)
        pkgbuild = pkg_dir / "PKGBUILD"
        pkgbuild.write_text("pkgname=mypkg\n")

        config = {"paths": {"pkgbuild_src_dir": str(src_dir)}}
        result = find_pkgbuild("mypkg", config=config)
        assert result == pkgbuild.resolve()

    def test_creates_pkgbuild_src_dir_before_clone(self, tmp_path, monkeypatch):
        """
        On a fresh system pkgbuild_src_dir may not exist yet. find_pkgbuild
        must create it before reaching pkgctl_checkout, otherwise
        subprocess(cwd=str(parent)) raises FileNotFoundError before pkgctl
        even starts.
        """
        from unittest.mock import patch
        from sysforge.primitives.config import find_pkgbuild
        monkeypatch.chdir(tmp_path)

        src_dir = tmp_path / "src"  # deliberately not created
        assert not src_dir.exists()

        # Stub the network/system probes: pretend htop is a repo package and
        # capture the parent dir state at the moment pkgctl_checkout is called.
        observed_parent_exists = {}

        def fake_pkgctl_checkout(name, dest, *, timeout=60):
            observed_parent_exists["before"] = dest.parent.exists()
            # Simulate pkgctl creating dest/PKGBUILD
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "PKGBUILD").write_text("pkgname=htop\n")

        with patch("sysforge.primitives.aur.is_repo_package", return_value=True), \
             patch("sysforge.primitives.aur.pkgctl_checkout",
                   side_effect=fake_pkgctl_checkout):
            config = {"paths": {"pkgbuild_src_dir": str(src_dir)}}
            result = find_pkgbuild("htop", config=config)

        assert observed_parent_exists.get("before") is True
        assert result == (src_dir / "htop" / "PKGBUILD").resolve()


# ---------------------------------------------------------------------------
# resolve_pkgbuild_src_dir — dual-key resolution + mismatch warning
# ---------------------------------------------------------------------------

class TestResolvePkgbuildSrcDir:
    @pytest.fixture(autouse=True)
    def _reset_warn_guard(self):
        import sysforge.primitives.config as cfg
        cfg._src_dir_mismatch_warned = False
        yield
        cfg._src_dir_mismatch_warned = False

    def test_build_key_wins(self):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        config = {"paths": {"pkgbuild_src_dir": "~/other"}}
        assert resolve_pkgbuild_src_dir(
            config, {"pkgbuild_src_dir": "~/src"}) == "~/src"

    def test_paths_fallback(self):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        config = {"paths": {"pkgbuild_src_dir": "~/src"}}
        assert resolve_pkgbuild_src_dir(config, {}) == "~/src"
        assert resolve_pkgbuild_src_dir(config, None) == "~/src"

    def test_neither_set_is_none(self):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        assert resolve_pkgbuild_src_dir({}, {}) is None

    def test_mismatch_warns_once(self, capsys):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        config = {"paths": {"pkgbuild_src_dir": "~/other"}}
        build_cfg = {"pkgbuild_src_dir": "~/src"}
        resolve_pkgbuild_src_dir(config, build_cfg)
        out = capsys.readouterr()
        assert "mismatch" in out.out + out.err
        # Second call is silent — per-run warning, not per-package.
        resolve_pkgbuild_src_dir(config, build_cfg)
        out2 = capsys.readouterr()
        assert "mismatch" not in out2.out + out2.err

    def test_equal_values_silent(self, capsys):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        config = {"paths": {"pkgbuild_src_dir": "~/src"}}
        assert resolve_pkgbuild_src_dir(
            config, {"pkgbuild_src_dir": "~/src"}) == "~/src"
        out = capsys.readouterr()
        assert "mismatch" not in out.out + out.err

    def test_one_key_set_silent(self, capsys):
        from sysforge.primitives.config import resolve_pkgbuild_src_dir
        resolve_pkgbuild_src_dir({}, {"pkgbuild_src_dir": "~/src"})
        out = capsys.readouterr()
        assert "mismatch" not in out.out + out.err


# ---------------------------------------------------------------------------
# resolve_repo_track (F41: [build] repo_track, groundwork for source=repo pinning)
# ---------------------------------------------------------------------------

def test_resolve_repo_track_default_is_stable():
    from sysforge.primitives.config import resolve_repo_track

    assert resolve_repo_track(None) == "stable"
    assert resolve_repo_track({}) == "stable"


def test_resolve_repo_track_main_opt_out():
    from sysforge.primitives.config import resolve_repo_track

    assert resolve_repo_track({"repo_track": "main"}) == "main"


def test_resolve_repo_track_unknown_value_warns_and_falls_back_to_stable(monkeypatch):
    import sysforge.primitives.config as cfg
    from sysforge.primitives.config import resolve_repo_track

    rec = _WarnRecorder()
    monkeypatch.setattr(cfg, "_log", rec)
    assert resolve_repo_track({"repo_track": "testing"}) == "stable"
    assert any("testing" in m and "repo_track" in m for m in rec.messages)


# ---------------------------------------------------------------------------
# resolve_enum (2.3.0-F8: shared known-vocabulary resolver)
# ---------------------------------------------------------------------------

class TestResolveEnum:
    def test_known_value_returned_unchanged(self):
        from sysforge.primitives.config import resolve_enum

        assert resolve_enum("b", {"a", "b"}, "a", key="[x] k") == "b"

    def test_default_returned_without_warning(self, monkeypatch):
        import sysforge.primitives.config as cfg
        from sysforge.primitives.config import resolve_enum

        rec = _WarnRecorder()
        monkeypatch.setattr(cfg, "_log", rec)
        assert resolve_enum("a", {"a", "b"}, "a", key="[x] k") == "a"
        assert rec.messages == []

    def test_unknown_value_warns_and_falls_back(self, monkeypatch):
        import sysforge.primitives.config as cfg
        from sysforge.primitives.config import resolve_enum

        rec = _WarnRecorder()
        monkeypatch.setattr(cfg, "_log", rec)
        assert resolve_enum("zzz", {"a", "b"}, "a", key="[x] k") == "a"
        assert len(rec.messages) == 1
        # The warning names the offending value, the config key, and the fallback.
        msg = rec.messages[0]
        assert "zzz" in msg and "[x] k" in msg and "a" in msg


# ---------------------------------------------------------------------------
# resolve_flag_default (config→flag precedence; mirrors [build] review logic)
# ---------------------------------------------------------------------------

def _ns(**kw):
    """Create an argparse Namespace for testing."""
    import argparse
    return argparse.Namespace(**kw)


def test_resolve_flag_default_cli_true_wins_over_missing_config():
    from sysforge.primitives.config import resolve_flag_default
    assert resolve_flag_default(_ns(abi_check=True), "abi_check", {}, "abi_check") is True


def test_resolve_flag_default_config_true_applies_when_flag_absent():
    from sysforge.primitives.config import resolve_flag_default
    assert resolve_flag_default(_ns(), "abi_check", {"abi_check": True}, "abi_check") is True


def test_resolve_flag_default_both_off_is_false():
    from sysforge.primitives.config import resolve_flag_default
    assert (
        resolve_flag_default(_ns(abi_check=False), "abi_check", {"abi_check": False}, "abi_check")
        is False
    )


def test_resolve_flag_default_all_absent_uses_fallback():
    from sysforge.primitives.config import resolve_flag_default
    assert resolve_flag_default(_ns(), "x", {}, "x") is False
    assert resolve_flag_default(_ns(), "x", {}, "x", fallback=True) is True


def test_resolve_flag_default_cli_true_overrides_config_false():
    from sysforge.primitives.config import resolve_flag_default
    assert (
        resolve_flag_default(_ns(abi_check=True), "abi_check", {"abi_check": False}, "abi_check")
        is True
    )
