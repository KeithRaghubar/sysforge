# SPDX-FileCopyrightText: 2026 Keith Raghubar
#
# SPDX-License-Identifier: MIT

"""Tests for the hardware-stage drift report (F3).

Covers the pure helpers behind "advise on detected drift before overwriting":
loading the existing [hardware] summary, diffing it against a fresh probe, and
the value-formatting used in the report lines.
"""
from __future__ import annotations

from sysforge.pipeline.stages import hardware


class TestDiffHardwareSummary:
    def _new(self):
        return {
            "cpu_vendor": "AuthenticAMD",
            "cpu_family": 25,
            "cpu_model": 33,
            "host_arch": "x86_64",
            "gpu_vendors": ["nvidia"],
            "nvme": True,
            "llvm_targets": ["X86", "AMDGPU"],
            "mesa_gallium_drivers": ["radeonsi", "zink"],
            "mesa_vulkan_drivers": ["swrast"],
        }

    def test_identical_summaries_report_no_drift(self):
        new = self._new()
        assert hardware._diff_hardware_summary(dict(new), new) == []

    def test_scalar_change_surfaces(self):
        old = self._new()
        new = self._new()
        new["cpu_model"] = 97
        diffs = hardware._diff_hardware_summary(old, new)
        assert diffs == ["  cpu_model: 33 → 97"]

    def test_list_change_renders_comma_joined(self):
        old = self._new()
        new = self._new()
        new["gpu_vendors"] = ["nvidia", "amd"]
        diffs = hardware._diff_hardware_summary(old, new)
        assert diffs == ["  gpu_vendors: [nvidia] → [nvidia, amd]"]

    def test_bool_change_renders_true_false(self):
        old = self._new()
        new = self._new()
        new["nvme"] = False
        diffs = hardware._diff_hardware_summary(old, new)
        assert diffs == ["  nvme: true → false"]

    def test_missing_old_key_reads_as_empty(self):
        old = self._new()
        del old["llvm_targets"]
        new = self._new()
        diffs = hardware._diff_hardware_summary(old, new)
        assert diffs == ["  llvm_targets: None → [X86, AMDGPU]"]

    def test_report_order_follows_field_table(self):
        old = self._new()
        new = self._new()
        new["mesa_vulkan_drivers"] = ["swrast", "nouveau"]
        new["cpu_vendor"] = "GenuineIntel"
        diffs = hardware._diff_hardware_summary(old, new)
        # cpu_vendor is earlier than mesa_vulkan_drivers in the field table.
        assert diffs[0].startswith("  cpu_vendor:")
        assert diffs[1].startswith("  mesa_vulkan_drivers:")


class TestLoadHardwareSummary:
    def test_absent_file_returns_none(self, tmp_path):
        assert hardware._load_hardware_summary(tmp_path / "nope.toml") is None

    def test_corrupt_file_returns_none(self, tmp_path):
        p = tmp_path / "hardware_profile.toml"
        p.write_text("this is = = not valid toml [[[")
        assert hardware._load_hardware_summary(p) is None

    def test_reads_hardware_table(self, tmp_path):
        p = tmp_path / "hardware_profile.toml"
        p.write_text(
            '[hardware]\n'
            'cpu_vendor = "AuthenticAMD"\n'
            'gpu_vendors = ["nvidia"]\n'
            'nvme = true\n'
            '[kconfig]\n'
            'CONFIG_MZEN3 = "y"\n'
        )
        table = hardware._load_hardware_summary(p)
        assert table == {
            "cpu_vendor": "AuthenticAMD",
            "gpu_vendors": ["nvidia"],
            "nvme": True,
        }

    def test_roundtrip_no_drift_against_written_profile(self, tmp_path):
        """A freshly-written profile re-read must report no drift vs. its source."""
        hw = {
            "cpu_vendor": "AuthenticAMD",
            "cpu_family": 25,
            "cpu_model": 33,
            "host_arch": "x86_64",
            "gpu_vendors": ["nvidia"],
            "nvme": True,
            "llvm_targets": ["X86", "AMDGPU"],
            "mesa_gallium_drivers": ["radeonsi", "zink"],
            "mesa_vulkan_drivers": ["swrast"],
        }
        p = tmp_path / "hardware_profile.toml"
        hardware._write_hardware_profile(p, hw, kconfig={}, dry_run=False)
        prior = hardware._load_hardware_summary(p)
        assert prior is not None
        assert hardware._diff_hardware_summary(prior, hw) == []
