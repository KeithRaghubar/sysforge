"""
test_auto_repair.py — coverage for the build-failure auto-repair registry.

Each scenario gets a fixture stdout stream and (where relevant) an empty
pkgbuild_dir. Detection is verified against the captured-output buffer;
repairs are exercised against monkeypatched subprocess shims so no real
external commands run.
"""
from pathlib import Path
from unittest.mock import patch

import pytest

from sysforge.primitives import auto_repair as ar


def _accum(text: str, srcdir: Path | None = None) -> ar.BuildOutputAccumulator:
    return ar.BuildOutputAccumulator(lines=text.splitlines(), srcdir=srcdir)


# ---------------------------------------------------------------------------
# vendored_deps_missing
# ---------------------------------------------------------------------------

class TestVendoredDeps:
    def test_detects_meson_wrap_pattern(self):
        a = _accum(
            "Configuring foo...\n"
            "../meson.build:1:0: ERROR: Subproject directory not found and "
            "Automatic wrap-based subproject downloading is disabled\n"
        )
        info = ar.VENDORED_DEPS.detect(a)
        assert info is not None
        assert info.detail["kind"] == "meson"

    def test_detects_empty_git_submodule(self, tmp_path):
        srcdir = tmp_path / "src"
        project = srcdir / "myproj"
        project.mkdir(parents=True)
        (project / ".gitmodules").write_text(
            "[submodule \"vendor/dep\"]\n"
            "    path = vendor/dep\n"
            "    url = https://example.com/dep.git\n"
        )
        # vendor/dep deliberately not created — the empty-submodule heuristic.
        a = _accum("build started\n", srcdir=srcdir)
        info = ar.VENDORED_DEPS.detect(a)
        assert info is not None
        assert info.detail["kind"] == "git_submodule"

    def test_no_detection_when_clean(self, tmp_path):
        a = _accum("everything fine\n", srcdir=tmp_path)
        assert ar.VENDORED_DEPS.detect(a) is None


    def test_repair_meson_runs_subprojects_download(self, tmp_path):
        # meson branch: locate the meson.build closest to ${srcdir} and run
        # `meson subprojects download` from that project root (lines 163-173).
        project = tmp_path / "src" / "myproj"
        project.mkdir(parents=True)
        (project / "meson.build").write_text("project('x')\n")
        info = ar.MatchInfo(detail={"kind": "meson"})
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/meson"), \
             patch("sysforge.primitives.auto_repair.subprocess.run") as run:
            ar._repair_vendored_deps(tmp_path, info)
        run.assert_called_once_with(
            ["/usr/bin/meson", "subprojects", "download"],
            cwd=str(project), check=True,
        )

    def test_repair_meson_raises_without_project(self, tmp_path):
        # No meson.build under srcdir → RuntimeError (line 166).
        (tmp_path / "src").mkdir()
        info = ar.MatchInfo(detail={"kind": "meson"})
        with pytest.raises(RuntimeError, match="no meson project"):
            ar._repair_vendored_deps(tmp_path, info)

    def test_repair_meson_raises_when_meson_absent(self, tmp_path):
        project = tmp_path / "src" / "myproj"
        project.mkdir(parents=True)
        (project / "meson.build").write_text("project('x')\n")
        info = ar.MatchInfo(detail={"kind": "meson"})
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value=None):
            with pytest.raises(RuntimeError, match="meson binary not on PATH"):
                ar._repair_vendored_deps(tmp_path, info)

    def test_repair_git_submodule_runs_update(self, tmp_path):
        # git_submodule branch: run `git submodule update --init --recursive`
        # from the recorded project root (lines 175-181).
        root = tmp_path / "src" / "myproj"
        root.mkdir(parents=True)
        info = ar.MatchInfo(detail={"kind": "git_submodule",
                                    "project_root": str(root)})
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/git"), \
             patch("sysforge.primitives.auto_repair.subprocess.run") as run:
            ar._repair_vendored_deps(tmp_path, info)
        run.assert_called_once_with(
            ["/usr/bin/git", "submodule", "update", "--init", "--recursive"],
            cwd=str(root), check=True,
        )

    def test_repair_git_submodule_raises_when_git_absent(self, tmp_path):
        info = ar.MatchInfo(detail={"kind": "git_submodule",
                                    "project_root": str(tmp_path)})
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value=None):
            with pytest.raises(RuntimeError, match="git binary not on PATH"):
                ar._repair_vendored_deps(tmp_path, info)

    def test_repair_raises_on_unknown_kind(self, tmp_path):
        # Defensive else-branch (lines 182-183).
        info = ar.MatchInfo(detail={"kind": "bogus"})
        with pytest.raises(RuntimeError, match="unknown kind 'bogus'"):
            ar._repair_vendored_deps(tmp_path, info)


# ---------------------------------------------------------------------------
# pgp_key_missing
# ---------------------------------------------------------------------------

class TestPgpKey:
    def test_detects_with_keyid(self):
        a = _accum(
            "Verifying source files with PGP signatures...\n"
            "    foo-1.0.tar.gz ... FAILED (unknown public key 1234ABCD5678EF90)\n"
            "==> ERROR: One or more PGP signatures could not be verified!\n"
            "gpg: Signature made Mon 01 Jan 2024\n"
            "gpg:                using RSA key 1234ABCD5678EF90DEAD\n"
            "gpg: Can't check signature: No public key 1234ABCD5678EF90DEAD\n"
        )
        info = ar.PGP_KEY.detect(a)
        assert info is not None
        assert info.detail["keyid"] == "1234ABCD5678EF90DEAD"

    def test_no_detection_without_pattern(self):
        a = _accum("nothing to see here\n")
        assert ar.PGP_KEY.detect(a) is None

    def test_repair_invokes_gpg_recv_keys(self, tmp_path):
        info = ar.MatchInfo(detail={"keyid": "DEADBEEFCAFEBABE"})
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/gpg"), \
             patch("sysforge.primitives.auto_repair.subprocess.run") as run:
            ar.PGP_KEY.repair(tmp_path, info)
        run.assert_called_once_with(
            ["/usr/bin/gpg", "--recv-keys", "DEADBEEFCAFEBABE"], check=True,
        )


# ---------------------------------------------------------------------------
# srcinfo_drift (pre-flight, not registry)
# ---------------------------------------------------------------------------

class TestSrcinfoDrift:
    def test_no_drift_when_files_match(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("pkgbase = foo\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="pkgbase = foo\n"):
            assert ar.detect_srcinfo_drift(tmp_path) is False

    def test_drift_when_files_differ(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("pkgbase = foo\npkgver = 1.0\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="pkgbase = foo\npkgver = 1.1\n"):
            assert ar.detect_srcinfo_drift(tmp_path) is True

    def test_no_drift_when_srcinfo_absent(self, tmp_path):
        # No .SRCINFO at all → not drift, just absence.
        assert ar.detect_srcinfo_drift(tmp_path) is False

    def test_repair_writes_fresh_srcinfo(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"):
            assert ar.repair_srcinfo_drift(tmp_path) is True
        assert (tmp_path / ".SRCINFO").read_text() == "fresh\n"

    def test_preflight_auto_repair_with_warning_logs(self, tmp_path, capsys):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"):
            assert ar.preflight_srcinfo(tmp_path, "auto_repair_with_warning") is True
        assert (tmp_path / ".SRCINFO").read_text() == "fresh\n"

    def test_preflight_abort_raises(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"):
            with pytest.raises(RuntimeError, match=".SRCINFO drift"):
                ar.preflight_srcinfo(tmp_path, "abort")
        # File NOT modified on abort.
        assert (tmp_path / ".SRCINFO").read_text() == "stale\n"

    # -- _printsrcinfo helper error paths (lines 236-251) -------------------

    def test_printsrcinfo_returns_none_when_makepkg_absent(self, tmp_path):
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value=None):
            assert ar._printsrcinfo(tmp_path) is None

    def test_printsrcinfo_returns_none_on_subprocess_error(self, tmp_path):
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/makepkg"), \
             patch("sysforge.primitives.auto_repair.subprocess.run",
                   side_effect=ar.subprocess.SubprocessError("boom")):
            assert ar._printsrcinfo(tmp_path) is None

    def test_printsrcinfo_returns_none_on_nonzero_returncode(self, tmp_path):
        class _R:
            returncode = 1
            stdout = "partial"
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/makepkg"), \
             patch("sysforge.primitives.auto_repair.subprocess.run",
                   return_value=_R()):
            assert ar._printsrcinfo(tmp_path) is None

    def test_printsrcinfo_returns_stdout_on_success(self, tmp_path):
        class _R:
            returncode = 0
            stdout = "pkgbase = foo\n"
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/makepkg"), \
             patch("sysforge.primitives.auto_repair.subprocess.run",
                   return_value=_R()):
            assert ar._printsrcinfo(tmp_path) == "pkgbase = foo\n"

    # -- detect/repair OSError arms (lines 261-277) ------------------------

    def test_detect_drift_false_when_printsrcinfo_fails(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value=None):
            assert ar.detect_srcinfo_drift(tmp_path) is False

    def test_detect_drift_false_when_srcinfo_unreadable(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"), \
             patch("pathlib.Path.read_text", side_effect=OSError("EACCES")):
            assert ar.detect_srcinfo_drift(tmp_path) is False

    def test_repair_returns_false_when_printsrcinfo_fails(self, tmp_path):
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value=None):
            assert ar.repair_srcinfo_drift(tmp_path) is False

    def test_repair_returns_false_when_write_fails(self, tmp_path):
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"), \
             patch("pathlib.Path.write_text", side_effect=OSError("EROFS")):
            assert ar.repair_srcinfo_drift(tmp_path) is False

    # -- preflight remaining behaviour arms (lines 296-305) ----------------

    def test_preflight_returns_false_when_regeneration_fails(self, tmp_path):
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair.detect_srcinfo_drift",
                   return_value=True), \
             patch("sysforge.primitives.auto_repair.repair_srcinfo_drift",
                   return_value=False):
            assert ar.preflight_srcinfo(tmp_path, "auto_repair") is False

    def test_preflight_plain_auto_repair_uses_info_log(self, tmp_path):
        # behaviour == "auto_repair" (no warning) still returns True after fix.
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"):
            assert ar.preflight_srcinfo(tmp_path, "auto_repair") is True
        assert (tmp_path / ".SRCINFO").read_text() == "fresh\n"

    def test_preflight_unhandled_behaviour_warns_and_skips(self, tmp_path):
        # Any other behaviour (e.g. warn_and_fallback) → drift left unrepaired.
        (tmp_path / ".SRCINFO").write_text("stale\n")
        with patch("sysforge.primitives.auto_repair._printsrcinfo",
                   return_value="fresh\n"):
            assert ar.preflight_srcinfo(tmp_path, "warn_and_fallback") is False
        # File NOT modified — this behaviour only logs.
        assert (tmp_path / ".SRCINFO").read_text() == "stale\n"


# ---------------------------------------------------------------------------
# checksum_mismatch (security-sensitive)
# ---------------------------------------------------------------------------

class TestChecksumMismatch:
    def test_detects_validity_check_failure(self):
        a = _accum(
            "==> Verifying source file checksums with sha256sums...\n"
            "    foo-1.0.tar.gz ... FAILED\n"
            "==> ERROR: One or more files did not pass the validity check!\n"
        )
        info = ar.CHECKSUM_MISMATCH.detect(a)
        assert info is not None
        assert "foo-1.0.tar.gz" in info.detail["failed_sources"]

    def test_no_detection_without_pattern(self):
        a = _accum("everything fine\n")
        assert ar.CHECKSUM_MISMATCH.detect(a) is None

    def test_repair_aborts_when_user_declines(self, tmp_path, monkeypatch):
        info = ar.MatchInfo(detail={"failed_sources": ["foo-1.0.tar.gz"]})
        monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
        with pytest.raises(RuntimeError, match="user declined"):
            ar.CHECKSUM_MISMATCH.repair(tmp_path, info)

    def test_repair_runs_updpkgsums_on_consent(self, tmp_path, monkeypatch):
        info = ar.MatchInfo(detail={"failed_sources": ["foo-1.0.tar.gz"]})
        monkeypatch.setattr("builtins.input", lambda _prompt="": "y")
        with patch("sysforge.primitives.auto_repair.shutil.which",
                   return_value="/usr/bin/updpkgsums"), \
             patch("sysforge.primitives.auto_repair.subprocess.run") as run:
            ar.CHECKSUM_MISMATCH.repair(tmp_path, info)
        run.assert_called_once_with(
            ["/usr/bin/updpkgsums"], cwd=str(tmp_path), check=True,
        )


# ---------------------------------------------------------------------------
# apply_first_match — the registry driver
# ---------------------------------------------------------------------------

class TestApplyFirstMatch:
    def test_returns_none_when_no_scenario_matches(self, tmp_path):
        accum = _accum("nothing matches\n", srcdir=tmp_path)
        result = ar.apply_first_match(
            ar.REGISTRY, accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda k: "auto_repair",
            batch=False,
            already_repaired=set(),
        )
        assert result is None

    def test_repaired_scenario_is_recorded_in_already_repaired(self, tmp_path):
        accum = _accum("Automatic wrap-based subproject downloading is disabled\n")
        seen: set[str] = set()
        # Build a synthetic registry whose repair is a no-op shim, since
        # RepairScenario is frozen and the real repair would shell out.
        noop = ar.RepairScenario(
            name="vendored_deps_missing",
            detect=ar.VENDORED_DEPS.detect,
            repair=lambda _d, _i: None,
            retry_phase="incremental",
            behaviour_key="vendored_deps_missing",
        )
        result = ar.apply_first_match(
            (noop,), accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda _k: "auto_repair",
            batch=False,
            already_repaired=seen,
        )
        assert result is not None
        assert result.repaired is True
        assert result.scenario == "vendored_deps_missing"
        assert "vendored_deps_missing" in seen

    def test_already_repaired_scenarios_are_skipped(self, tmp_path):
        accum = _accum("Automatic wrap-based subproject downloading is disabled\n")
        result = ar.apply_first_match(
            ar.REGISTRY, accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda k: "auto_repair",
            batch=False,
            already_repaired={"vendored_deps_missing"},
        )
        # vendored_deps already repaired; nothing else matches → None.
        assert result is None

    def test_batch_mode_aborts_prompt_user_scenario(self, tmp_path):
        accum = _accum(
            "==> Verifying source file checksums with sha256sums...\n"
            "    foo-1.0.tar.gz ... FAILED\n"
            "==> ERROR: One or more files did not pass the validity check!\n"
        )
        result = ar.apply_first_match(
            ar.REGISTRY, accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda k: "prompt_user",
            batch=True,
            already_repaired=set(),
        )
        assert result is not None
        assert result.aborted is True
        assert result.skipped_reason == "batch-no-prompt"

    def test_abort_behaviour_short_circuits(self, tmp_path):
        accum = _accum("Automatic wrap-based subproject downloading is disabled\n")
        result = ar.apply_first_match(
            ar.REGISTRY, accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda k: "abort",
            batch=False,
            already_repaired=set(),
        )
        assert result is not None
        assert result.aborted is True
        assert result.repaired is False

    def test_repair_failure_is_surfaced(self, tmp_path):
        accum = _accum("Automatic wrap-based subproject downloading is disabled\n")
        def failing_repair(_d, _i):
            raise RuntimeError("simulated repair failure")
        synthetic = ar.RepairScenario(
            name="vendored_deps_missing",
            detect=ar.VENDORED_DEPS.detect,
            repair=failing_repair,
            retry_phase="incremental",
            behaviour_key="vendored_deps_missing",
        )
        result = ar.apply_first_match(
            (synthetic,), accum,
            pkgbuild_dir=tmp_path,
            behaviour_for=lambda _k: "auto_repair",
            batch=False,
            already_repaired=set(),
        )
        assert result is not None
        assert result.repaired is False
        assert "simulated repair failure" in (result.skipped_reason or "")
