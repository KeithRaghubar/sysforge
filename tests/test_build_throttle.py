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
    assert argv[:6] == ["systemd-run", "--scope", "--user", "--quiet",
                        "-p", "CPUQuota=600%"]
    assert "Nice=19" in argv
    assert "IOSchedulingClass=idle" in argv


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
