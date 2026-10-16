"""update_plan — the pure plan model behind update's confirmation (3.1.0-F4)."""
from types import SimpleNamespace

from sysforge import update_plan as up
from sysforge.primitives.build_sandbox import SandboxPolicy
from sysforge.primitives.net_policy import NetPolicy
from sysforge.primitives.pkgbuild_review import ReviewPolicy


def _r(pkgbase, inst="1-1", new="2-1"):
    return SimpleNamespace(pkgbase=pkgbase, installed_ver=inst, pkgbuild_ver=new)


def test_posture_line(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    line = up.posture_line(
        ReviewPolicy(),
        SandboxPolicy(enabled=True, clean=True, update=False, local_repo=True),
        NetPolicy(frozen=True, thawed=frozenset({"b", "a"}), aur_enabled=False))
    assert line == ("review: prompt (trusted: repo) | sandbox: on (clean, local-repo)"
                    " | freeze: on (thawed: a, b) | AUR: off")
    assert "sandbox: off" in up.posture_line(
        ReviewPolicy(), SandboxPolicy(enabled=False),
        NetPolicy(frozen=False, thawed=frozenset()))


def test_build_plan_rows_and_marks():
    plan = up.build_plan(
        to_build=[_r("htop"), _r("mesa", "1-1", "1-1")],
        pending_pacman_upgrade=[_r("firefox")],
        sysupgrade_pending=True,
        promoted={"mesa": "flags drifted"},
        review_marks={"htop"},
        sandbox_marks={"mesa": "host build: profile override"},
        posture="P",
    )
    assert [(r.verb, r.name) for r in plan.rows] == [
        ("rebuild", "htop"), ("rebuild", "mesa"),
        ("upgrade", "firefox"), ("sysupgrade", "pacman -Syu")]
    assert plan.rows[0].marks == ("review",)
    assert plan.rows[1].detail == "flags drifted"
    assert plan.rows[1].marks == ("host build: profile override",)
    assert not plan.is_empty()


def test_render_plan(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    plan = up.Plan(posture="P", rows=(up.PlanRow("rebuild", "htop", "1 → 2", ("review",)),))
    lines = up.render_plan(plan)
    assert "Plan: P" in lines[0] and "[UPDATE]" in lines[0]
    assert lines[1].split() == ["rebuild", "htop", "1", "→", "2", "(review)"]


def test_empty_plan():
    assert up.Plan(posture="P", rows=()).is_empty()


def test_profiles_override_sandbox():
    assert not up.profiles_override_sandbox({"profiles": {"x": {"cflags": "-O2"}}})
    assert up.profiles_override_sandbox({"profiles": {"x": {"sandbox_builds": False}}})
    assert up.profiles_override_sandbox({"defaults": {"sandbox_builds": True}})
