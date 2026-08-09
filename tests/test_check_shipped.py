"""
test_check_shipped.py - tests for tools/check_shipped.py.

Happy-path test runs the checker against the real repo and expects clean.
The drift tests build minimal synthetic trees in tmp_path and invoke the
checker as a subprocess with --repo=<tmp_path>, so the checker reads
shipped data from the synthetic tree while importing sysforge from this
working repo as usual.
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/check_shipped.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_checker(repo=None, args=()):
    """Invoke the checker as a subprocess; return CompletedProcess.

    Subprocesses inherit the shell's SYSFORGE_CONFIG_DIR so the manpage
    regen produces the same output `make man` would. For drift tests with
    --repo=<tmp_path>, leave SYSFORGE_CONFIG_DIR untouched - the loaders
    use the env, but most drift checks operate directly on `repo` so this
    doesn't matter.
    """
    cmd = [sys.executable, str(SCRIPT)]
    if repo is not None:
        cmd.append(f"--repo={repo}")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, cwd=REPO)


def copy_shipped_tree(dst: Path) -> Path:
    """Copy etc/sysforge/, tests/data/etc/sysforge/, PKGBUILD, PKGBUILD-git,
    pyproject.toml, README.md, DESIGN.md, completions/, man/, and
    etc/pacman.d/ into dst so the synthetic repo passes every check group.

    Returns the destination path.
    """
    for sub in ("etc/sysforge", "tests/data/etc/sysforge",
                "etc/pacman.d/hooks", "completions", "man",
                "tools"):
        src = REPO / sub
        if src.is_dir():
            shutil.copytree(src, dst / sub)
    for name in ("PKGBUILD", "PKGBUILD-git", "sysforge.install",
                 "pyproject.toml", "README.md", "DESIGN.md"):
        if (REPO / name).exists():
            shutil.copyfile(REPO / name, dst / name)
    return dst


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestRealRepo:
    def test_main_passes(self):
        """The current main branch must pass every shipped check."""
        res = run_checker()
        assert res.returncode == 0, (
            f"check-shipped failed on main:\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )

    def test_list_groups(self):
        res = run_checker(args=["--list"])
        assert res.returncode == 0
        groups = res.stdout.split()
        assert {"configs", "pkgbuild", "pkgbuild_parity", "provisioning",
                "hooks", "completions", "versions", "manpage"} <= set(groups)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------


class TestConfigDrift:
    def test_unknown_top_level_section_in_sysforge_toml(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        toml = repo / "etc/sysforge/sysforge.toml"
        toml.write_text(toml.read_text() + "\n[frobnicate]\nfoo = 1\n")

        res = run_checker(repo=repo, args=["--check=configs"])
        assert res.returncode == 1
        assert "unknown top-level section [frobnicate]" in res.stdout

    def test_missing_live_counterpart(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        (repo / "tests/data/etc/sysforge/sysforge.toml").unlink()

        res = run_checker(repo=repo, args=["--check=configs"])
        assert res.returncode == 1
        assert "no counterpart in tests/data/etc/sysforge/" in res.stdout

    def test_bootstrap_toml_skipped_from_live_parity(self, tmp_path):
        # bootstrap.toml has no tests/data counterpart by design; the live
        # parity check must not flag this.
        repo = copy_shipped_tree(tmp_path)
        res = run_checker(repo=repo, args=["--check=configs"])
        assert res.returncode == 0
        assert "bootstrap.toml" not in res.stdout or "no counterpart" not in res.stdout


class TestPkgbuildDrift:
    def test_missing_backup_for_installed_etc_file(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # Drop the sysforge.toml line from backup=()
        new = text.replace(
            "    'etc/sysforge/sysforge.toml'\n", "", 1,
        )
        assert new != text
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "not declared in backup=()" in res.stdout

    def test_install_source_missing(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        # Delete a file the PKGBUILD's install lines reference.
        (repo / "etc/sysforge/sysforge.toml").unlink()
        # The configs group would also fire on this; restrict to pkgbuild.
        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "install source not found" in res.stdout

    def test_missing_install_scriptlet_fails(self, tmp_path):
        # The PKGBUILD declares install=sysforge.install (F1); the scriptlet
        # file must ship alongside it.
        repo = copy_shipped_tree(tmp_path)
        (repo / "sysforge.install").unlink()
        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "install scriptlet not found" in res.stdout

    def test_release_omits_install_scriptlet_fails(self, tmp_path):
        # 1.2.0-B2: the install= scriptlet is read by makepkg from the build
        # startdir, so release.sh must copy it into the AUR repos. Drop every
        # reference and the publish-parity guard must fire.
        repo = copy_shipped_tree(tmp_path)
        rel = repo / "tools" / "release.sh"
        rel.write_text(rel.read_text().replace("sysforge.install", "PKGBUILD"))
        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "not copied to the AUR repos" in res.stdout

    def test_skip_on_signature_source_passes(self, tmp_path):
        # The real PKGBUILD pairs a SKIP with the detached .asc source — that is
        # legitimate and must pass the sha256 placeholder rule.
        repo = copy_shipped_tree(tmp_path)
        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 0, res.stdout

    def test_skip_on_non_signature_source_fails(self, tmp_path):
        # Move the SKIP onto the tarball source (swap the two sha256sums entries)
        # so SKIP pairs with a hashable source — that must be flagged.
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # The tarball hash rotates every release — extract whatever the shipped
        # PKGBUILD carries rather than hardcoding a stale value.
        new, n = re.subn(
            r"sha256sums=\('([0-9a-f]{64})'\n(\s*)'SKIP'\)",
            r"sha256sums=('SKIP'\n\g<2>'\g<1>')",
            text, count=1,
        )
        assert n == 1, "sha256sums=(<hash> SKIP) pair not found in shipped PKGBUILD"
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "SKIP" in res.stdout and "non-signature source" in res.stdout

    def test_missing_validpgpkeys_fails(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # Drop whichever validpgpkeys line the PKGBUILD currently ships (the
        # dev sentinel or a real fingerprint) — the checker must flag its
        # absence regardless of the value that was there.
        new, n = re.subn(r"^validpgpkeys=\([^)]*\)\n", "", text, count=1,
                         flags=re.MULTILINE)
        assert n == 1, "validpgpkeys=(...) line not found in shipped PKGBUILD"
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "validpgpkeys" in res.stdout

    def test_malformed_validpgpkeys_fails(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # Replace the current validpgpkeys value with a non-fingerprint token.
        new, n = re.subn(r"^validpgpkeys=\([^)]*\)",
                         "validpgpkeys=('not-a-fingerprint')", text, count=1,
                         flags=re.MULTILINE)
        assert n == 1, "validpgpkeys=(...) line not found in shipped PKGBUILD"
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=pkgbuild"])
        assert res.returncode == 1
        assert "40-hex fingerprint" in res.stdout


class TestPkgbuildParity:
    def test_optdepends_drift(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        # Mutate PKGBUILD-git optdepends without changing PKGBUILD.
        git_pkgbuild = repo / "PKGBUILD-git"
        text = git_pkgbuild.read_text()
        new = text.replace(
            "'bash-completion: bash tab completions'",
            "'bash-completion: bash tab completions (git only)'",
        )
        assert new != text
        git_pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=pkgbuild_parity"])
        assert res.returncode == 1
        assert "optdepends differs" in res.stdout


class TestProvisioning:
    def test_wrong_group_fails(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # Flip /var/cache/sysforge back to the old world-writable root:root form.
        new = text.replace(
            "'d /var/cache/sysforge 2775 root sysforge -\\n'",
            "'d /var/cache/sysforge 0777 root root -\\n'",
        )
        assert new != text
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=provisioning"])
        assert res.returncode == 1
        assert "/var/cache/sysforge" in res.stdout

    def test_missing_sysusers_group_fails(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        for name in ("PKGBUILD", "PKGBUILD-git"):
            pkgbuild = repo / name
            text = pkgbuild.read_text()
            new = text.replace("printf 'g sysforge -\\n'", "printf 'g other -\\n'")
            assert new != text
            pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=provisioning"])
        assert res.returncode == 1
        assert "does not declare group 'sysforge'" in res.stdout

    def test_tmpfiles_parity_between_pkgbuilds(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        git = repo / "PKGBUILD-git"
        text = git.read_text()
        # Drop the llvm-pgo line from the VCS PKGBUILD only.
        new = text.replace(
            "        printf 'd /var/cache/sysforge/llvm-pgo 2775 root sysforge -\\n'\n",
            "",
        )
        assert new != text
        git.write_text(new)

        res = run_checker(repo=repo, args=["--check=provisioning"])
        assert res.returncode == 1
        assert "differs between PKGBUILD and PKGBUILD-git" in res.stdout


class TestHookDrift:
    def test_unknown_kind(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        hook = repo / "etc/pacman.d/hooks/sysforge-kernel.hook"
        text = hook.read_text()
        new = text.replace(
            "pacman-hook-helper.sh kernel",
            "pacman-hook-helper.sh frobnicate",
        )
        assert new != text
        hook.write_text(new)

        res = run_checker(repo=repo, args=["--check=hooks"])
        assert res.returncode == 1
        assert "'frobnicate' not documented in helper" in res.stdout


class TestCompletionDrift:
    def test_zsh_missing_verb(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        zsh = repo / "completions/_sysforge"
        text = zsh.read_text()
        # Delete every reference to the `update` verb. Since the parser has
        # update, this should produce a "verb 'update' missing" finding.
        new = text.replace("update", "XXXXX")
        zsh.write_text(new)

        res = run_checker(repo=repo, args=["--check=completions"])
        assert res.returncode == 1
        assert "verb 'update' missing" in res.stdout

    def test_zsh_stale_verb(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        zsh = repo / "completions/_sysforge"
        text = zsh.read_text()
        # Add a top-level dispatch entry for a verb that doesn't exist in the
        # parser. The regex matches `frobnicate) _sysforge_frobnicate ;;`.
        injected = text.replace(
            "      esac\n      ;;\n    args)",
            "      esac\n      ;;\n    args)\n        # injected drift\n",
        )
        # Easier: inject inside the args dispatch case
        injected = text.replace(
            "build)    _sysforge_build    ;;",
            "build)    _sysforge_build    ;;\n        frobnicate) _sysforge_frobnicate ;;",
        )
        assert injected != text
        zsh.write_text(injected)

        res = run_checker(repo=repo, args=["--check=completions"])
        assert res.returncode == 1
        assert "stale verb entry 'frobnicate'" in res.stdout


class TestVersionDrift:
    def test_pkgbuild_pkgver_mismatch(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        pkgbuild = repo / "PKGBUILD"
        text = pkgbuild.read_text()
        # Bump pkgver out of sync with pyproject. Read the current pkgver
        # from the shipped PKGBUILD so the test survives release bumps.
        import re as _re
        m = _re.search(r"^pkgver=(\S+)$", text, flags=_re.M)
        assert m, "PKGBUILD has no pkgver= line"
        current = m.group(1)
        new = text.replace(f"pkgver={current}", "pkgver=9.9.9", 1)
        assert new != text
        pkgbuild.write_text(new)

        res = run_checker(repo=repo, args=["--check=versions"])
        assert res.returncode == 1
        assert f"pkgver=9.9.9 != pyproject {current}" in res.stdout

    def test_design_marker_filters_literal_placeholder(self, tmp_path):
        # DESIGN.md embeds `<!--version-->vX.Y.Z<!--/version-->` literally
        # in prose documenting the release-marker pattern. That must not
        # produce a finding.
        repo = copy_shipped_tree(tmp_path)
        res = run_checker(repo=repo, args=["--check=versions"])
        assert res.returncode == 0


# ---------------------------------------------------------------------------
# Driver behaviour
# ---------------------------------------------------------------------------


class TestDriver:
    def test_warn_flag_returns_zero(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        # Inject a real drift.
        toml = repo / "etc/sysforge/sysforge.toml"
        toml.write_text(toml.read_text() + "\n[frobnicate]\nx = 1\n")

        res_strict = run_checker(repo=repo, args=["--check=configs"])
        assert res_strict.returncode == 1

        res_warn = run_checker(repo=repo, args=["--check=configs", "--warn"])
        assert res_warn.returncode == 0
        # Still printed the finding for the user to see.
        assert "frobnicate" in res_warn.stdout

    def test_unknown_group(self):
        res = run_checker(args=["--check=does_not_exist"])
        assert res.returncode == 2
        assert "unknown group" in res.stderr


# ---------------------------------------------------------------------------
# Allowlist <-> stage-code parity (anti-drift for _KNOWN_TOP_KEYS)
#
# The configs group only checks that shipped keys are a *subset* of the
# allowlist. These tests close the reverse gap: the allowlist must track the
# keys the stage code actually reads, and every read key must be documented in
# the shipped file. This is the durable guard for the base_config regression
# (read by the kernel stage, documented for users, but missing from the
# allowlist -> would have blocked the release gate the moment it was adopted).
# ---------------------------------------------------------------------------

import importlib.util  # noqa: E402
import re as _re2  # noqa: E402
import tomllib  # noqa: E402


def _load_check_shipped():
    spec = importlib.util.spec_from_file_location("check_shipped", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so the module's @dataclass can resolve its own
    # __module__ in sys.modules (Python 3.14 dataclass annotation lookup).
    sys.modules["check_shipped"] = mod
    spec.loader.exec_module(mod)
    return mod


def _stage_reads(rel_path: str, accessor: str) -> set[str]:
    """Keys read from a config dict via `<accessor>.get("<key>")` in a module."""
    src = (REPO / rel_path).read_text(encoding="utf-8")
    pat = _re2.escape(accessor) + r'\.get\(\s*"([a-z_][a-z0-9_]*)"'
    return set(_re2.findall(pat, src))


def _documented_keys(rel_path: str) -> set[str]:
    """Mirror of check_shipped._documented_keys for a repo-relative file."""
    cs = _load_check_shipped()
    return cs._documented_keys(REPO / rel_path)


class TestAllowlistCodeParity:
    KERNEL = "sysforge/pipeline/stages/kernel.py"
    TOOLCHAIN = "sysforge/pipeline/stages/toolchain.py"

    def test_kernel_allowlist_matches_stage_reads(self):
        """_KNOWN_TOP_KEYS|_KNOWN_SECTIONS for kernel.toml must equal exactly
        the keys KernelStage reads via kernel_cfg.get(...). Catches both an
        un-allowlisted read and a stale allowlist entry."""
        cs = _load_check_shipped()
        allow = (cs._KNOWN_TOP_KEYS["kernel.toml"]
                 | cs._KNOWN_SECTIONS["kernel.toml"])
        reads = _stage_reads(self.KERNEL, "kernel_cfg")
        assert reads == allow, (
            f"kernel allowlist drift:\n"
            f"  read but not allowlisted: {sorted(reads - allow)}\n"
            f"  allowlisted but not read: {sorted(allow - reads)}"
        )

    def test_toolchain_allowlist_matches_stage_reads(self):
        """Toolchain reads partly flow through helpers (resolve_pgo_store takes
        tcfg), so the guard is asymmetric: every direct tcfg.get(...) read must
        be allowlisted, and every allowlisted key must be either read directly
        or resolved by a known helper."""
        cs = _load_check_shipped()
        allow_top = cs._KNOWN_TOP_KEYS["toolchain.toml"]
        allow_sec = cs._KNOWN_SECTIONS["toolchain.toml"]
        reads = _stage_reads(self.TOOLCHAIN, "tcfg")
        # pgo_store is read inside primitives.makepkg_pgo.resolve_pgo_store(tcfg),
        # not directly in the stage; assert that indirection really exists.
        helper = (REPO / "sysforge/primitives/makepkg_pgo.py").read_text()
        assert 'get("pgo_store")' in helper
        # drift_detect is read on the *update* path (config.resolve_drift_detect),
        # not by the toolchain stage — it configures update's same-variant drift
        # fingerprint. Assert that indirection really exists.
        cfg = (REPO / "sysforge/primitives/config.py").read_text()
        assert 'get("drift_detect")' in cfg
        helper_resolved = {"pgo_store", "drift_detect"}
        assert reads <= (allow_top | allow_sec), (
            f"toolchain reads not allowlisted: "
            f"{sorted(reads - (allow_top | allow_sec))}")
        stale = allow_top - reads - helper_resolved
        assert not stale, f"stale toolchain allowlist entries: {sorted(stale)}"

    def test_kernel_reads_are_documented_in_shipped(self):
        """Every key the kernel stage reads must be documented (active or as a
        commented example) in the shipped kernel.toml, so no real option is
        invisible to users."""
        reads = _stage_reads(self.KERNEL, "kernel_cfg")
        documented = _documented_keys("etc/sysforge/kernel.toml")
        assert reads <= documented, (
            f"kernel keys read but undocumented in shipped kernel.toml: "
            f"{sorted(reads - documented)}")

    def test_toolchain_reads_are_documented_in_shipped(self):
        """Same for the toolchain stage's tcfg.get(...) reads."""
        reads = _stage_reads(self.TOOLCHAIN, "tcfg")
        documented = _documented_keys("etc/sysforge/toolchain.toml")
        assert reads <= documented, (
            f"toolchain keys read but undocumented in shipped toolchain.toml: "
            f"{sorted(reads - documented)}")


class TestFullyPopulatedShippedConfigs:
    """Every documented top-level key example in the flat stage configs must be
    a real, allowlisted key. Uncomments single-`#` top-level `# key = value`
    examples (skipping section bodies and alternative duplicates), parses with
    tomllib, and asserts allowlist membership. Independently catches the
    base_config-style omission."""

    def _uncomment_top_level(self, text: str) -> str:
        out: list[str] = []
        in_section = False
        seen: set[str] = set()
        for line in text.splitlines():
            stripped = line.strip()
            # Track section context (active or commented) so we never lift a
            # section-body example to the top level.
            sec = _re2.match(r"^#?\s*\[\[?[a-z_]", stripped)
            if sec:
                in_section = True
                out.append(line)
                continue
            m = _re2.match(r"^#\s*([a-z_][a-z0-9_]*)\s*=\s*\S", stripped)
            if m and not in_section:
                key = m.group(1)
                if key in seen:  # skip alternative duplicate examples
                    out.append(line)
                    continue
                seen.add(key)
                out.append(stripped[1:].lstrip())
                continue
            out.append(line)
        return "\n".join(out)

    def _check(self, name: str):
        cs = _load_check_shipped()
        text = (REPO / "etc/sysforge" / name).read_text(encoding="utf-8")
        data = tomllib.loads(self._uncomment_top_level(text))
        allow_top = cs._KNOWN_TOP_KEYS[name]
        allow_sec = cs._KNOWN_SECTIONS[name]
        for key, val in data.items():
            if isinstance(val, (dict, list)):
                assert key in allow_sec, f"{name}: section [{key}] not allowlisted"
            else:
                assert key in allow_top, f"{name}: key {key!r} not allowlisted"

    def test_kernel_documented_examples_are_allowlisted(self):
        self._check("kernel.toml")

    def test_toolchain_documented_examples_are_allowlisted(self):
        self._check("toolchain.toml")


# ---------------------------------------------------------------------------
# 2.2.0-B3: the manpage guard must compare content, not scdoc-version bytes.
#
# scdoc 1.11.4 -> 1.11.5 re-escaped every `-` as `\-` and bumped the generator
# comment. Both are functionally inert in roff, but a byte-compare turns them
# into hundreds of phantom diff lines for any contributor on a different scdoc.
# ---------------------------------------------------------------------------

_ROFF_1_11_4 = """\
.\\" Generated by scdoc 1.11.4
.nh
.ad l
.TH "sysforge" "1" "2026-07-01"
.SH NAME
sysforge - profile-driven package builder
.SH OPTIONS
--cache-report
"""

_ROFF_1_11_5 = """\
.\\" Generated by scdoc 1.11.5
.nh
.ad l
.TH "sysforge" "1" "2026\\-07\\-25"
.SH NAME
sysforge \\- profile\\-driven package builder
.SH OPTIONS
\\-\\-cache\\-report
"""


class TestManpageNormalization:
    def test_scdoc_version_skew_compares_equal(self):
        """Same content rendered by two scdoc versions must normalize equal."""
        cs = _load_check_shipped()
        assert cs._normalize_roff(_ROFF_1_11_4) == cs._normalize_roff(_ROFF_1_11_5)

    def test_generator_comment_version_is_normalized(self):
        cs = _load_check_shipped()
        assert "1.11.4" not in cs._normalize_roff(_ROFF_1_11_4)
        assert "1.11.5" not in cs._normalize_roff(_ROFF_1_11_5)

    def test_th_date_still_normalized(self):
        cs = _load_check_shipped()
        assert "2026" not in cs._normalize_roff(_ROFF_1_11_5)

    def test_real_content_drift_still_differs(self):
        """Normalization must not mask an actual CLI-surface change."""
        cs = _load_check_shipped()
        renamed = _ROFF_1_11_5.replace("cache\\-report", "cache\\-summary")
        assert cs._normalize_roff(_ROFF_1_11_5) != cs._normalize_roff(renamed)

    def test_added_option_still_differs(self):
        cs = _load_check_shipped()
        extra = _ROFF_1_11_4 + "--abi-check\n"
        assert cs._normalize_roff(_ROFF_1_11_4) != cs._normalize_roff(extra)


class TestManpageScdocSkewEndToEnd:
    """Full-checker proof: a page committed by a *different* scdoc build still
    passes, while a genuine CLI-surface change still fails."""

    @staticmethod
    def _link_package(repo: Path) -> None:
        """The manpage check shells out to gen_options.py, which imports
        sysforge.cli; copy_shipped_tree omits the package, so link it in."""
        (repo / "sysforge").symlink_to(REPO / "sysforge")

    @staticmethod
    def _downgrade_to_1_11_4(repo: Path) -> None:
        """Rewrite the committed page as scdoc 1.11.4 would have rendered it:
        unescaped hyphens and the older generator banner."""
        page = repo / "man/sysforge.1"
        text = page.read_text(encoding="utf-8")
        text = text.replace("\\-", "-")
        text = text.replace("Generated by scdoc 1.11.5",
                            "Generated by scdoc 1.11.4")
        page.write_text(text, encoding="utf-8")

    def test_older_scdoc_rendering_still_passes(self, tmp_path):
        repo = copy_shipped_tree(tmp_path)
        self._link_package(repo)
        self._downgrade_to_1_11_4(repo)

        res = run_checker(repo=repo, args=["--check=manpage"])
        if "scdoc not installed" in res.stdout:
            return  # renderer absent: guard self-skips, nothing to assert
        assert res.returncode == 0, res.stdout

    def test_content_drift_still_fails_under_skew(self, tmp_path):
        """Normalization must not mask real drift even on a skewed page."""
        repo = copy_shipped_tree(tmp_path)
        self._link_package(repo)
        self._downgrade_to_1_11_4(repo)
        page = repo / "man/sysforge.1"
        text = page.read_text(encoding="utf-8")
        # _downgrade_to_1_11_4 has already unescaped the hyphens, so match the
        # bare form. Assert the edit lands — a no-op replace would make this
        # test vacuous.
        drifted = text.replace("build and maintenance suite",
                               "build and maintenance kit")
        assert drifted != text, "drift fixture matched nothing"
        page.write_text(drifted, encoding="utf-8")

        res = run_checker(repo=repo, args=["--check=manpage"])
        if "scdoc not installed" in res.stdout:
            return
        assert res.returncode == 1
        assert "stale" in res.stdout


# ---------------------------------------------------------------------------
# Group: config_comments
# ---------------------------------------------------------------------------
#
# Regression guard for STD4: a shipped comment naming a config file or
# section that does not exist. The two historical instances that motivated
# this (a renamed flag_profiles.toml, a never-existent [cache] section) were
# already fixed by hand, so this ships green against the real repo - every
# failing case below is built in a tmp_path fixture, not against etc/sysforge/.

check_shipped = _load_check_shipped()


def _fixture_header(name: str) -> str:
    """The documentation header every shipped config carries, sized for a
    synthetic fixture. Built from check_shipped's own banner constant so the
    two can't drift apart."""
    return (
        "# " + "=" * 77 + "\n"
        f"# {name} — synthetic fixture\n"
        "#\n"
        '# Everything down to line 7 ("END OF HEADER") is documentation;\n'
        "# the settings themselves start below it.\n"
        "#\n"
        f"{check_shipped._END_OF_HEADER}\n"
    )


def _write_config_dir(root: Path, files: dict[str, str], *, header: bool = True) -> Path:
    """Build a synthetic repo with etc/sysforge/<name>.toml files.

    Each file gets the standard documentation header unless ``header=False``,
    so the fixtures match the shape check_config_comments expects of a real
    shipped config.
    """
    cfg = root / "etc" / "sysforge"
    cfg.mkdir(parents=True)
    for name, body in files.items():
        text = (_fixture_header(name) + body) if header else body
        (cfg / name).write_text(text, encoding="utf-8")
    return root


def test_config_comments_flags_nonexistent_toml_reference(tmp_path):
    """A comment naming a config file that does not ship is drift."""
    repo = _write_config_dir(tmp_path, {
        "sysforge.toml": "# see flag_profiles.toml for the profile list\n[build]\n",
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("flag_profiles.toml" in f.message for f in findings)
    assert all(f.group == "config_comments" for f in findings)


def test_config_comments_allows_existing_toml_reference(tmp_path):
    """A comment naming a config file that does ship is fine."""
    repo = _write_config_dir(tmp_path, {
        "sysforge.toml": "# see profiles.toml for the profile list\n[build]\n",
        "profiles.toml": "[defaults]\n",
    })
    assert check_shipped.check_config_comments(repo) == []


def test_config_comments_ignores_non_toml_filenames(tmp_path):
    """Comments legitimately mention non-sysforge files."""
    repo = _write_config_dir(tmp_path, {
        "sysforge.toml": "# merged over the system /etc/makepkg.conf baseline\n[build]\n",
    })
    assert check_shipped.check_config_comments(repo) == []


def test_config_comments_flags_nonexistent_section_reference(tmp_path):
    """A comment documenting a section the schema has never had is drift."""
    repo = _write_config_dir(tmp_path, {
        "packages.toml": "# the [cache] block tunes the download cache\n[build]\n",
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("[cache]" in f.message for f in findings)


def test_header_marker_accepts_a_correct_pointer(tmp_path):
    """The stock header — banner present, pointer citing its real line."""
    repo = _write_config_dir(tmp_path, {"packages.toml": "[build]\n"})
    assert check_shipped.check_config_comments(repo) == []


def test_header_marker_flags_stale_pointer(tmp_path):
    """A paragraph added to the header pushes the banner down; the pointer
    that still cites the old line is the failure this guard exists for."""
    repo = _write_config_dir(tmp_path, {"packages.toml": "[build]\n"})
    path = repo / "etc" / "sysforge" / "packages.toml"
    lines = path.read_text(encoding="utf-8").splitlines()
    lines.insert(3, "# an added paragraph")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    findings = check_shipped.check_config_comments(repo)
    assert any("END OF HEADER is at line 8" in f.message for f in findings)


def test_header_marker_flags_missing_banner(tmp_path):
    repo = _write_config_dir(tmp_path, {"packages.toml": "[build]\n"}, header=False)
    findings = check_shipped.check_config_comments(repo)
    assert any("no END OF HEADER banner" in f.message for f in findings)


def test_header_marker_flags_banner_without_pointer(tmp_path):
    """A banner nobody is told about is only half the affordance."""
    repo = _write_config_dir(
        tmp_path,
        {"packages.toml": f"# packages.toml\n{check_shipped._END_OF_HEADER}\n[build]\n"},
        header=False,
    )
    findings = check_shipped.check_config_comments(repo)
    assert any("no header pointer" in f.message for f in findings)


def test_header_marker_flags_duplicate_banners(tmp_path):
    repo = _write_config_dir(tmp_path, {"packages.toml": "[build]\n"})
    path = repo / "etc" / "sysforge" / "packages.toml"
    path.write_text(
        path.read_text(encoding="utf-8") + f"{check_shipped._END_OF_HEADER}\n",
        encoding="utf-8",
    )
    findings = check_shipped.check_config_comments(repo)
    assert any("2 END OF HEADER banners" in f.message for f in findings)


def test_config_comments_allows_cross_file_section_reference(tmp_path):
    """`sysforge.toml's [build] block` resolves against sysforge.toml."""
    repo = _write_config_dir(tmp_path, {
        "profiles.toml": "# See sysforge.toml's [build] block for full semantics.\n[defaults]\n",
        "sysforge.toml": "[build]\n",
    })
    assert check_shipped.check_config_comments(repo) == []


def test_config_comments_clean_against_real_repo():
    """Regression guard: the shipped configs currently have no dangling refs."""
    assert check_shipped.check_config_comments(check_shipped.REPO) == []


# ---------------------------------------------------------------------------
# Hatch-scoping regressions (review round 1): every escape hatch above must
# be scoped to the clause/bracket it licenses, not the whole comment block -
# a licensing phrase anywhere in a comment paragraph must not disable the
# section sub-check for an unrelated bracket in the same paragraph.
# ---------------------------------------------------------------------------


def test_config_comments_external_file_hatch_does_not_blanket_block(tmp_path):
    """An /etc/*.conf mention in one clause must not hide a real [cache] typo
    in a different clause of the same comment."""
    repo = _write_config_dir(tmp_path, {
        "packages.toml": (
            "# merged over the system /etc/makepkg.conf baseline, "
            "the [cache] block tunes downloads\n[build]\n"
        ),
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("[cache]" in f.message for f in findings)


def test_config_comments_non_shipped_stem_hatch_does_not_blanket_block(tmp_path):
    """A hardware_profile.toml mention in one clause must not hide a bogus
    section in a different clause of the same comment."""
    repo = _write_config_dir(tmp_path, {
        "packages.toml": (
            "# hardware stage writes hardware_profile.toml, and also uses "
            "the bogus [totally_fake] section\n[build]\n"
        ),
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("[totally_fake]" in f.message for f in findings)


def test_config_comments_owner_binding_is_per_mention_not_per_block(tmp_path):
    """Naming sysforge.toml elsewhere in the paragraph must not lend its
    allowlist to a [mesa] bracket that is actually attached to packages.toml."""
    repo = _write_config_dir(tmp_path, {
        "packages.toml": (
            "# see sysforge.toml for global logging config.\n"
            "# packages.toml has no [mesa] section, that lives elsewhere -- this is WRONG,\n"
            "# mesa belongs to sysforge.toml not packages.toml\n[build]\n"
        ),
        "sysforge.toml": "[mesa]\n",
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("[mesa]" in f.message for f in findings)


def test_grammar_docs_flags_comment_missing_a_form(tmp_path):
    """cpu_quota's comment must show both the N% and the fractional form."""
    repo = _write_config_dir(tmp_path, {
        # documents only the absolute form - the 2.1.0-F6 fraction is missing
        "sysforge.toml": (
            '# cpu_quota — hard CPU ceiling: "600%" (100% = one core)\n'
            '# cpu_quota = "600%"\n'
            '[build]\n'
        ),
    })
    findings = check_shipped.check_config_comments(repo)
    assert any("cpu_quota" in f.message and "fraction" in f.message.lower()
               for f in findings)


def test_grammar_docs_passes_when_all_forms_documented(tmp_path):
    repo = _write_config_dir(tmp_path, {
        "sysforge.toml": (
            '# cpu_quota — hard CPU ceiling, in either of two forms:\n'
            '#   "600%"  absolute, where 100% = one core\n'
            '#   0.75    a decimal fraction of this host\'s total cores\n'
            '# cpu_quota = "600%"\n'
            '[build]\n'
        ),
    })
    grammar = [f for f in check_shipped.check_config_comments(repo)
               if "cpu_quota" in f.message]
    assert grammar == []


def test_key_comment_block_is_key_scoped_not_paragraph_scoped(tmp_path):
    """_key_comment_block must stop at the PREVIOUS key's own anchor line, not
    walk through a bare '#' spacer into that key's whole paragraph.

    Regression for the found bug: nice's paragraph happens to contain the word
    "idle" ("...otherwise idle..."), and shipped configs separate key
    paragraphs with bare '#' spacer lines rather than blank lines - so an
    upward walk that only stops at a non-comment line runs straight past the
    spacer into nice's prose. ionice's own lines here deliberately do NOT
    contain "idle" (scrubbed, mirroring the real repro against
    etc/sysforge/sysforge.toml) so a correct, key-scoped block sees no "idle"
    token and must flag it as under-documented.
    """
    repo = _write_config_dir(tmp_path, {
        "sysforge.toml": (
            "# nice - scheduling niceness. Lets the build yield CPU the "
            "instant you need it, with no throughput loss when the machine "
            "is otherwise idle.\n"
            "# nice = 19\n"
            "#\n"
            "# ionice - IO scheduling class: best-effort or the other one.\n"
            "# ionice = \"x\"\n"
            "[build]\n"
        ),
    })
    findings = check_shipped.check_config_comments(repo)
    ionice_findings = [f for f in findings if "ionice" in f.message]
    # Correct (key-scoped) behaviour: ionice's own paragraph never says
    # "idle", so it must be reported missing - even though "idle" appears
    # nearby in nice's paragraph. Pre-fix, the paragraph-wide walk absorbs
    # nice's "idle" into ionice's block and this assertion fails (the bug:
    # ionice's enum goes wholly undocumented and the guard stays silent).
    assert any("idle" in f.message for f in ionice_findings), \
        [f.message for f in findings]


def test_grammar_docs_table_keys_exist_in_shipped_configs():
    """A _GRAMMAR_DOCS entry for a key no shipped config documents is itself
    drift - the table must not rot in the other direction."""
    for (fname, key) in check_shipped._GRAMMAR_DOCS:
        path = check_shipped.REPO / "etc" / "sysforge" / fname
        assert path.exists(), f"{fname} in _GRAMMAR_DOCS does not ship"
        # A plain substring match is not enough: prose can mention a key's
        # name without an actual assignment anchor (active or commented-out
        # example) for _key_comment_block to walk up from - and the runtime
        # checker's per-key `continue` (see check_config_comments) silently
        # skips exactly that case. Require the real anchor here instead.
        assert check_shipped._key_comment_block(path.read_text(encoding="utf-8"), key), \
            f"{key!r} in _GRAMMAR_DOCS has no assignment anchor in {fname}"


def test_grammar_docs_all_forms_present_in_real_repo():
    """Every _GRAMMAR_DOCS entry's own (key-scoped) block shows every required
    token, checked directly against the table rather than via the combined
    check_config_comments() run - test_config_comments_clean_against_real_repo
    already asserts that end-to-end, so this asserts the grammar-coverage
    claim independently instead of re-running the same call under a different
    docstring."""
    for (fname, key), required in check_shipped._GRAMMAR_DOCS.items():
        path = check_shipped.REPO / "etc" / "sysforge" / fname
        block = check_shipped._key_comment_block(path.read_text(encoding="utf-8"), key)
        missing = [tok for tok in required if tok not in block]
        assert not missing, f"{fname}:{key} missing forms {missing} in block:\n{block}"


# ---------------------------------------------------------------------------
# completion_widths (3.0.0-B2)
# ---------------------------------------------------------------------------


def _write_completion(tmp_path, body):
    comp = tmp_path / "completions"
    comp.mkdir(parents=True, exist_ok=True)
    (comp / "_sysforge").write_text(body, encoding="utf-8")
    return tmp_path


def test_completion_widths_clean_against_real_repo():
    """The shipped zsh completion fits an 80-column listing."""
    assert check_shipped.check_completion_widths(check_shipped.REPO) == []


def test_completion_widths_flags_over_budget_arguments_spec(tmp_path):
    """A description too long for its block's budget is drift."""
    repo = _write_completion(tmp_path, (
        "_verb() {\n"
        "  _arguments \\\n"
        "    '--short[fine]' \\\n"
        f"    '--long[{'x' * 90}]'\n"
        "}\n"
    ))
    findings = check_shipped.check_completion_widths(repo)
    assert [f.message for f in findings if "--long" in f.message]
    assert all(f.group == "completion_widths" for f in findings)
    assert not [f for f in findings if "--short" in f.message]


def test_completion_widths_budget_shrinks_with_longest_option_name(tmp_path):
    """Budget is COLUMNS - longest match in the block - 4, so a long flag
    name in the block tightens every description beside it."""
    desc = "y" * 60
    loose = _write_completion(tmp_path / "loose", (
        "_verb() {\n  _arguments \\\n" f"    '--a[{desc}]'\n" "}\n"
    ))
    tight = _write_completion(tmp_path / "tight", (
        "_verb() {\n  _arguments \\\n"
        f"    '--a[{desc}]' \\\n"
        "    '--a-very-long-option-name[ok]'\n"
        "}\n"
    ))
    assert check_shipped.check_completion_widths(loose) == []
    assert [f for f in check_shipped.check_completion_widths(tight)
            if "--a:" in f.message]


def test_completion_widths_covers_describe_arrays(tmp_path):
    """_describe arrays are their own compadd call and are budgeted too."""
    repo = _write_completion(tmp_path, (
        "_verb() {\n"
        "  local commands=(\n"
        f"    'build:{'z' * 90}'\n"
        "  )\n"
        "  _describe 'command' commands\n"
        "}\n"
    ))
    findings = check_shipped.check_completion_widths(repo)
    assert [f for f in findings if "_describe" in f.message]


def test_completion_widths_reads_brace_form_option_names(tmp_path):
    """'(-q --quiet)'{-q,--quiet}'[desc]' contributes both names, and the
    longer one sets the block's padding."""
    repo = _write_completion(tmp_path, (
        "_verb() {\n"
        "  _arguments \\\n"
        "    '(-q --quiet)'{-q,--quiet}'[fine]'\n"
        "}\n"
    ))
    blocks = check_shipped._completion_blocks(
        (repo / "completions/_sysforge").read_text(encoding="utf-8"))
    names = {n for _, _, entries in blocks for _, n, _ in entries}
    assert names == {"-q", "--quiet"}
