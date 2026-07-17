"""
test_build_throttle.py — build CPU/IO throttling primitive.

Covers the three surfaces of build_throttle:
  * resolve_throttle  — [build] defaults, per-profile override, validation/coercion.
  * wrapper_argv      — nice/ionice front-ends, systemd-run --scope for cpu_quota,
                        graceful degradation when a tool is missing.
  * apply_jobs_to_makeflags — rewrite/append the -j token.
"""
import sysforge.primitives.build_throttle as bt
from sysforge.primitives.build_throttle import (
    BuildThrottle,
    apply_jobs_to_makeflags,
    resolve_child_mem_cap,
    resolve_throttle,
    wrapper_argv,
)


# ---------------------------------------------------------------------------
# resolve_throttle


def test_resolve_global_defaults():
    cfg = {"build": {"nice": 19, "ionice": "idle", "cpu_quota": "600%", "jobs": 6}}
    t = resolve_throttle({}, cfg)
    assert t == BuildThrottle(nice=19, ionice="idle", cpu_quota="600%", jobs=6)


def test_resolve_unset_is_noop():
    t = resolve_throttle({}, {})
    assert t.is_noop
    assert t == BuildThrottle()


def test_resolve_profile_overrides_per_key():
    cfg = {"build": {"nice": 19, "jobs": 6}}
    # Profile pins nice=5 and adds cpu_quota; jobs falls back to the global default.
    prof = {"nice": 5, "cpu_quota": "400%"}
    t = resolve_throttle(prof, cfg)
    assert t.nice == 5
    assert t.cpu_quota == "400%"
    assert t.jobs == 6
    assert t.ionice is None


def test_resolve_nice_out_of_range_clamped():
    assert resolve_throttle({}, {"build": {"nice": 50}}).nice == 19
    assert resolve_throttle({}, {"build": {"nice": -3}}).nice == 0


def test_resolve_drops_malformed_values():
    cfg = {"build": {"nice": "fast", "ionice": "realtime",
                     "cpu_quota": "lots", "jobs": 0}}
    t = resolve_throttle({}, cfg)
    assert t.is_noop


# ---------------------------------------------------------------------------
# resolve_throttle — relative cpu_quota (2.1.0-F6)


def test_resolve_cpu_quota_fraction_of_cores(monkeypatch):
    # 0.5 of a 16-core host → 8 cores' worth == 800%.
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 16)
    assert resolve_throttle({}, {"build": {"cpu_quota": 0.5}}).cpu_quota == "800%"


def test_resolve_cpu_quota_fraction_string_form(monkeypatch):
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 8)
    assert resolve_throttle({}, {"build": {"cpu_quota": "0.25"}}).cpu_quota == "200%"


def test_resolve_cpu_quota_absolute_percent_unchanged(monkeypatch):
    # An explicit N% is portable-as-is; the host core count is irrelevant.
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 16)
    assert resolve_throttle({}, {"build": {"cpu_quota": "600%"}}).cpu_quota == "600%"


def test_resolve_cpu_quota_fraction_floors_to_one_percent(monkeypatch):
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 1)
    assert resolve_throttle({}, {"build": {"cpu_quota": 0.001}}).cpu_quota == "1%"


def test_resolve_cpu_quota_fraction_nonpositive_dropped(monkeypatch):
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 16)
    assert resolve_throttle({}, {"build": {"cpu_quota": "0.0"}}).cpu_quota is None


# ---------------------------------------------------------------------------
# resolve_throttle — cpu_quota overshoot warning (2.3.0-F7)


class _WarnRecorder:
    """Minimal stand-in for the throttle logger that records warn() messages."""

    def __init__(self):
        self.messages: list[str] = []

    def warn(self, msg):
        self.messages.append(msg)


def test_cpu_quota_percent_overshoot_warns_but_keeps(monkeypatch):
    # An absolute N% above cpu_count*100 is kept (systemd clamps effectively) but
    # the user gets a signal — warn-only, never drop or clamp.
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 16)
    rec = _WarnRecorder()
    monkeypatch.setattr(bt, "_throttle_log", rec)
    t = resolve_throttle({}, {"build": {"cpu_quota": "2000%"}})
    assert t.cpu_quota == "2000%"
    assert any("2000%" in m and "16" in m for m in rec.messages)


def test_cpu_quota_fraction_overshoot_warns_but_keeps(monkeypatch):
    # The fraction form can also overshoot (frac > 1): 2.0 * 8 cores = 1600% > 800%.
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 8)
    rec = _WarnRecorder()
    monkeypatch.setattr(bt, "_throttle_log", rec)
    t = resolve_throttle({}, {"build": {"cpu_quota": "2.0"}})
    assert t.cpu_quota == "1600%"
    assert any("1600%" in m for m in rec.messages)


def test_cpu_quota_within_core_count_no_warn(monkeypatch):
    monkeypatch.setattr(bt.os, "cpu_count", lambda: 16)
    rec = _WarnRecorder()
    monkeypatch.setattr(bt, "_throttle_log", rec)
    assert resolve_throttle({}, {"build": {"cpu_quota": "600%"}}).cpu_quota == "600%"
    assert rec.messages == []


# ---------------------------------------------------------------------------
# resolve_throttle — run-scoped override (2.1.0-F5)


def test_resolve_override_bypass_forces_noop():
    cfg = {"build": {"nice": 19, "ionice": "idle", "cpu_quota": "600%", "jobs": 4}}
    assert resolve_throttle({}, cfg, override="bypass").is_noop


def test_resolve_override_boost_raises_priority():
    cfg = {"build": {"nice": 19, "ionice": "idle"}}
    t = resolve_throttle({}, cfg, override="boost")
    assert t.nice is not None and t.nice < 0  # below the unprivileged floor, on purpose
    assert t.ionice == "best-effort"
    assert t.cpu_quota is None and t.jobs is None


def test_resolve_override_defaults_to_run_global(monkeypatch):
    # An unpassed override falls back to the process-global set at CLI startup.
    monkeypatch.setattr(bt, "_RUN_OVERRIDE", "bypass")
    assert resolve_throttle({}, {"build": {"nice": 19}}).is_noop


def test_set_run_override_sets_module_state():
    try:
        bt.set_run_override("boost")
        assert bt._RUN_OVERRIDE == "boost"
    finally:
        bt.set_run_override(None)
    assert bt._RUN_OVERRIDE is None


# ---------------------------------------------------------------------------
# wrapper_argv


def test_wrapper_nice_and_ionice(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(nice=19, ionice="idle"))
    assert argv == ["nice", "-n", "19", "ionice", "-c", "3"]


def test_wrapper_ionice_best_effort(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(ionice="best-effort"))
    assert argv == ["ionice", "-c", "2"]


def test_wrapper_cpu_quota_uses_systemd_scope(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(cpu_quota="600%", nice=19, ionice="idle"))
    # Scope carries ONLY the cgroup CPUQuota; nice/ionice ride as front-end
    # commands inside the scope (Nice=/IOSchedulingClass= are invalid on a
    # --scope unit, which the caller execs directly).
    assert argv == ["systemd-run", "--scope", "--user", "--quiet",
                    "-p", "CPUQuota=600%", "nice", "-n", "19", "ionice", "-c", "3"]
    assert not any(a.startswith("Nice=") for a in argv)
    assert not any(a.startswith("IOSchedulingClass=") for a in argv)


def test_wrapper_cpu_quota_falls_back_without_systemd_run(monkeypatch):
    # systemd-run missing, nice/ionice present → degrade to the soft front-ends.
    monkeypatch.setattr(bt.shutil, "which",
                        lambda name: None if name == "systemd-run" else "/usr/bin/" + name)
    argv = wrapper_argv(BuildThrottle(cpu_quota="600%", nice=19))
    assert argv == ["nice", "-n", "19"]


def test_wrapper_missing_tools_degrade_to_empty(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: None)
    assert wrapper_argv(BuildThrottle(nice=19, ionice="idle")) == []


def test_wrapper_noop_is_empty():
    assert wrapper_argv(BuildThrottle()) == []


# ---------------------------------------------------------------------------
# apply_jobs_to_makeflags


def test_jobs_none_unchanged():
    assert apply_jobs_to_makeflags("-j16", None) == "-j16"


def test_jobs_rewrites_short_form():
    assert apply_jobs_to_makeflags("-j16", 6) == "-j6"


def test_jobs_rewrites_long_form_normalising_to_short():
    assert apply_jobs_to_makeflags("--jobs=16", 6) == "-j6"


def test_jobs_appends_when_absent():
    assert apply_jobs_to_makeflags("--quiet", 6) == "--quiet -j6"


def test_jobs_appends_to_empty():
    assert apply_jobs_to_makeflags("", 6) == "-j6"


def test_jobs_nproc_form_appended_and_wins():
    # An unexpanded -j$(nproc) has no numeric match, so -jN is appended; make
    # honours the last -j, so the cap wins.
    out = apply_jobs_to_makeflags("-j$(nproc)", 6)
    assert out == "-j$(nproc) -j6"


# ---------------------------------------------------------------------------
# mem_limit (2.2.0-F4) — _coerce_mem_limit, resolve, is_noop
_GiB = 1024 ** 3
_MiB = 1024 ** 2


def test_resolve_mem_limit_binary_suffixes():
    assert resolve_throttle({}, {"build": {"mem_limit": "24G"}}).mem_limit_bytes == 24 * _GiB
    assert resolve_throttle({}, {"build": {"mem_limit": "512M"}}).mem_limit_bytes == 512 * _MiB


def test_resolve_mem_limit_bare_bytes():
    assert resolve_throttle({}, {"build": {"mem_limit": "1048576"}}).mem_limit_bytes == 1048576


def test_resolve_mem_limit_junk_dropped_with_warning():
    t = resolve_throttle({}, {"build": {"mem_limit": "lots"}})
    assert t.mem_limit_bytes is None


def test_resolve_mem_limit_nonpositive_dropped():
    assert resolve_throttle({}, {"build": {"mem_limit": "0G"}}).mem_limit_bytes is None
    assert resolve_throttle({}, {"build": {"mem_limit": "-8G"}}).mem_limit_bytes is None


def test_resolve_mem_limit_unset_is_none():
    assert resolve_throttle({}, {"build": {"nice": 19}}).mem_limit_bytes is None


def test_mem_limit_only_is_not_noop():
    assert not BuildThrottle(mem_limit_bytes=24 * _GiB).is_noop


def test_mem_limit_profile_override(monkeypatch):
    monkeypatch.setattr(bt, "_RUN_OVERRIDE", None)
    cfg = {"build": {"mem_limit": "24G"}}
    t = resolve_throttle({"mem_limit": "8G"}, cfg)
    assert t.mem_limit_bytes == 8 * _GiB


# ---------------------------------------------------------------------------
# wrapper_argv — MemoryMax injection on the systemd-run scope path


def test_wrapper_injects_memory_max_with_cpu_quota(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(cpu_quota="600%", mem_limit_bytes=24 * _GiB))
    assert "-p" in argv
    assert f"MemoryMax={24 * _GiB}" in argv


def test_wrapper_injects_memory_max_mem_limit_alone(monkeypatch):
    # 2.3.0-F9: mem_limit alone, with systemd-run available, now earns a scope
    # carrying MemoryMax (kernel-enforced, hierarchical) — not the escapable
    # RLIMIT_AS preexec. No CPUQuota is emitted when cpu_quota is unset.
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(mem_limit_bytes=24 * _GiB))
    assert argv == ["systemd-run", "--scope", "--user", "--quiet",
                    "-p", f"MemoryMax={24 * _GiB}"]
    assert not any(a.startswith("CPUQuota=") for a in argv)


def test_wrapper_mem_limit_alone_falls_back_without_systemd_run(monkeypatch):
    # 2.3.0-F9: no systemd-run → no scope; the RLIMIT_AS preexec (resolve_child_mem_cap)
    # is the pure non-systemd fallback, so wrapper_argv emits no MemoryMax here.
    monkeypatch.setattr(bt.shutil, "which",
                        lambda name: None if name == "systemd-run" else "/usr/bin/" + name)
    argv = wrapper_argv(BuildThrottle(mem_limit_bytes=24 * _GiB))
    assert argv == []


def test_wrapper_no_memory_max_when_mem_limit_unset(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", lambda _: "/usr/bin/" + _)
    argv = wrapper_argv(BuildThrottle(cpu_quota="600%"))
    assert not any(a.startswith("MemoryMax=") for a in argv)


# ---------------------------------------------------------------------------
# resolve_child_mem_cap — arbitrates rlimit vs. cgroup so caps never double-apply.
# 2.3.0-F9: the scope owns the cap whenever mem_limit is set AND systemd-run is
# present (independent of cpu_quota); otherwise the rlimit fallback owns it.
_HAS_SYSTEMD_RUN = staticmethod(lambda name: "/usr/bin/" + name)
_NO_SYSTEMD_RUN = staticmethod(lambda name: None if name == "systemd-run" else "/usr/bin/" + name)


def test_child_mem_cap_none_when_scope_owns_cap(monkeypatch):
    # systemd-run present → the scope's MemoryMax owns the ceiling; rlimit on the
    # client would not reach the scoped payload, so it must be suppressed.
    monkeypatch.setattr(bt.shutil, "which", _HAS_SYSTEMD_RUN)
    assert resolve_child_mem_cap(BuildThrottle(cpu_quota="600%", mem_limit_bytes=24 * _GiB)) is None
    assert resolve_child_mem_cap(BuildThrottle(mem_limit_bytes=24 * _GiB)) is None


def test_child_mem_cap_bytes_when_no_systemd_run(monkeypatch):
    # No systemd-run → no scope carries MemoryMax, so the rlimit fallback owns the
    # cap even when cpu_quota is set (closes the silent-drop gap F9 targets).
    monkeypatch.setattr(bt.shutil, "which", _NO_SYSTEMD_RUN)
    assert resolve_child_mem_cap(BuildThrottle(mem_limit_bytes=24 * _GiB)) == 24 * _GiB
    assert resolve_child_mem_cap(
        BuildThrottle(cpu_quota="600%", mem_limit_bytes=24 * _GiB)) == 24 * _GiB


def test_child_mem_cap_none_when_mem_limit_unset(monkeypatch):
    monkeypatch.setattr(bt.shutil, "which", _HAS_SYSTEMD_RUN)
    assert resolve_child_mem_cap(BuildThrottle(cpu_quota="600%")) is None
    assert resolve_child_mem_cap(BuildThrottle()) is None
