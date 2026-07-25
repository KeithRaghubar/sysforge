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
