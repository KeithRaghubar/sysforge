"""
test_json_output.py — the ``--json`` contract (3.4.0-F7).

Each verb's document validates against its committed schema under
``tests/data/json-schema/``, stdout holds nothing but that document even at
``-vv``, and a doctor skip appears as a skip. The schemas are real JSON Schema
(draft 2020-12) for anyone scripting against the output; ``_validate`` below
implements just the subset they use, so the suite needs no new dependency.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_args import cli_args
from sysforge import log
from sysforge.primitives import json_out

_SCHEMAS = Path(__file__).parent / "data" / "json-schema"

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "integer": int, "null": type(None),
}


def _validate(doc, schema, path="$"):
    """Raise AssertionError naming the first violation (subset validator)."""
    if "const" in schema:
        assert doc == schema["const"], f"{path}: {doc!r} != const {schema['const']!r}"
    if "enum" in schema:
        assert doc in schema["enum"], f"{path}: {doc!r} not in {schema['enum']}"
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        ok = any(isinstance(doc, _TYPES[t]) and not (t == "integer" and isinstance(doc, bool))
                 for t in types)
        assert ok, f"{path}: {type(doc).__name__} is not {types}"
    if isinstance(doc, dict):
        for key in schema.get("required", []):
            assert key in doc, f"{path}: missing required {key!r}"
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = set(doc) - set(props)
            assert not extra, f"{path}: unexpected {sorted(extra)}"
        for key, sub in props.items():
            if key in doc:
                _validate(doc[key], sub, f"{path}.{key}")
    if isinstance(doc, list) and "items" in schema:
        for i, item in enumerate(doc):
            _validate(item, schema["items"], f"{path}[{i}]")


def _schema(kind):
    return json.loads((_SCHEMAS / f"{kind}.schema.json").read_text())


def _only_document(out: str) -> dict:
    """stdout must parse as exactly one JSON document."""
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise AssertionError(
            f"stdout is not one JSON document ({e}): {out[:400]!r}") from e


@pytest.fixture
def verbose():
    saved = log.get_verbosity()
    log.set_verbosity(2)
    yield
    log.set_verbosity(saved)


def test_validator_rejects_a_bad_document():
    with pytest.raises(AssertionError):
        _validate({"schema": 2, "kind": "doctor", "scope": "system", "axes": []},
                  _schema("doctor"))


def test_emit_stamps_schema_and_kind(capsys):
    with json_out.reserved_stdout():
        print("narration")  # must not reach stdout
        json_out.emit("doctor", {"scope": "system", "axes": []})
    cap = capsys.readouterr()
    assert _only_document(cap.out) == {"schema": 1, "kind": "doctor",
                                       "scope": "system", "axes": []}
    assert "narration" in cap.err


# ---------------------------------------------------------------------------
# state list
# ---------------------------------------------------------------------------

def test_state_list_json_validates(tmp_path, capsys, monkeypatch, verbose):
    from sysforge.primitives import pacman
    from sysforge.primitives.build_state import BuildState
    from sysforge.primitives.opt_out import VIA_FORGET, OptOutList
    from sysforge.state_cmd import StateListVerb
    from sysforge.verbs.runner import run_verb

    bs = BuildState(tmp_path)
    bs.record("htop", "3.4", "1", "1", "htop", tmp_path, build_mode="source_built")
    bs.save()
    OptOutList(tmp_path).add("mesa", ["mesa"], via=VIA_FORGET)
    monkeypatch.setattr(pacman, "get_foreign_packages", lambda: {"foo-git": "1-1"})
    args = cli_args("state", "list", "--json", state_dir=str(tmp_path))
    assert run_verb(StateListVerb(), args) == 0
    doc = _only_document(capsys.readouterr().out)
    _validate(doc, _schema("state-list"))
    assert doc["packages"][0]["version"] == "1:3.4-1"
    assert doc["opted_out"][0]["pkgbase"] == "mesa"
    assert doc["untracked_foreign"] == [{"name": "foo-git", "version": "1-1"}]


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _doctor_args(**kw):
    return SimpleNamespace(**{
        "config": {}, "findings_only": False, "json": True, "packages": [],
        "state_dir": None, **{a: False for a in (
            "abi", "distro", "toolchain", "rust", "cache", "hardware", "graphics",
            "gfxperf", "pacman", "state", "boot", "restart", "storage",
            "services", "audio", "network", "integrity", "all")}, **kw})


def test_doctor_json_validates_and_carries_skips(capsys, monkeypatch, verbose):
    from sysforge.doctor import DoctorSystemVerb
    from sysforge.primitives import network_probe
    from sysforge.verbs.runner import run_verb
    monkeypatch.setattr(network_probe, "_run", lambda cmd: None)
    rc = run_verb(DoctorSystemVerb(), _doctor_args(network=True))
    doc = _only_document(capsys.readouterr().out)
    _validate(doc, _schema("doctor"))
    (axis,) = doc["axes"]
    assert axis["name"] == "network"
    # The skip is a skip, never a finding.
    assert {s["check"] for s in axis["roster"]["skipped"]} == {"default_route"}
    assert not any(f["check_id"] == "default_route" for f in axis["findings"])
    assert rc == (1 if axis["errors"] else 0)


def test_doctor_json_exit_code_matches_the_report(monkeypatch, capsys):
    from sysforge import doctor
    from sysforge.primitives import diagnostics as diag
    bad = diag.Finding("network", diag.SEV_ERROR, "x", "broken")
    monkeypatch.setattr(doctor, "_collect_network_findings",
                        lambda: diag.AxisResult([bad], diag.Roster(ran=["x"])))
    args = _doctor_args(network=True)
    rc_json = doctor.cmd_doctor_system(args)
    capsys.readouterr()
    args.json = False
    assert rc_json == doctor.cmd_doctor_system(args) == 1


def test_doctor_pkg_refuses_json_with_apply():
    from sysforge.doctor import DoctorPkgVerb
    pre = DoctorPkgVerb().pre_check(cli_args("doctor", "pkg", "--json", "--apply"))
    assert pre.blocker and pre.exit_code == 2


# ---------------------------------------------------------------------------
# update --dry-run
# ---------------------------------------------------------------------------

def _update_args(**kw):
    return SimpleNamespace(**{
        "state_dir": None, "dry_run": True, "devel": False, "offline": True,
        "no_toolchain_preflight": True, "no_pkg_log": True, "persist_log": False,
        "log_dir": None, "profile_conf": None, "cache_report": False,
        "packages": None, "json": True, **kw})


def test_update_dry_run_json_validates(update_scenario, capsys, verbose, monkeypatch):
    # cli.py puts log in dry-run mode for --dry-run, which sends every ui()
    # line to stdout: exactly what the --json guard must keep off it.
    monkeypatch.setattr(log, "_DRY_RUN", True)
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.record("htop", "3.3.0", "1")
    update_scenario.run(_update_args(), installed={"htop": "3.3.0-1"},
                        foreign={"htop": "3.3.0-1"}, via_runner=True)
    assert update_scenario.exit_code == 0
    doc = _only_document(capsys.readouterr().out)
    _validate(doc, _schema("update-dry-run"))
    (pkg,) = doc["packages"]
    assert (pkg["pkgbase"], pkg["action"], pkg["available"]) == (
        "htop", "NEEDS_REBUILD", "3.4.1-1")


def test_update_json_without_dry_run_is_refused(capsys):
    from sysforge.update import UpdateVerb
    from sysforge.verbs.runner import run_verb
    assert run_verb(UpdateVerb(), _update_args(dry_run=False)) == 2
    assert capsys.readouterr().out == ""


def test_nested_guard_keeps_the_real_stdout(capsys):
    """cli.main and run_verb both enter the guard; the inner entry must not
    mistake the already-redirected stream for the real stdout."""
    with json_out.reserved_stdout():
        print("startup log")
        with json_out.reserved_stdout():
            print("verb narration")
            json_out.emit("state-list", {"packages": [], "opted_out": [],
                                         "untracked_foreign": []})
    cap = capsys.readouterr()
    _validate(_only_document(cap.out), _schema("state-list"))
    assert "startup log" in cap.err and "verb narration" in cap.err


def test_update_dry_run_json_lists_flag_drift_promotion(
        update_scenario, capsys, verbose, monkeypatch):
    """3.5.0-B5: a dry run with --rebuild-on-flag-drift reports the package
    the real run would rebuild, not UP_TO_DATE."""
    from sysforge import update
    from sysforge.primitives.flag_drift import STATUS_DRIFTED, FlagDriftResult
    monkeypatch.setattr(log, "_DRY_RUN", True)
    monkeypatch.setattr(update, "resolve_flag_drift",
                        lambda *a, **k: FlagDriftResult(status=STATUS_DRIFTED,
                                                        diffs=["  CFLAGS: a -> b"]))
    update_scenario.add_pkg("htop", "pkgname=htop\npkgver=3.4.1\npkgrel=1\n")
    update_scenario.record("htop", "3.4.1", "1")
    update_scenario.run(_update_args(rebuild_on_flag_drift=True),
                        installed={"htop": "3.4.1-1"},
                        foreign={"htop": "3.4.1-1"}, via_runner=True)
    assert update_scenario.exit_code == 0
    doc = _only_document(capsys.readouterr().out)
    (pkg,) = doc["packages"]
    assert (pkg["pkgbase"], pkg["action"]) == ("htop", "NEEDS_REBUILD")


def test_config_check_json_validates(tmp_path, capsys, verbose):
    """3.5.0-F18: one document on stdout, every finding in it, same exit code."""
    import shutil

    from sysforge.config_cmd import ConfigCheckVerb
    from sysforge.primitives import config_schema
    from sysforge.verbs.runner import run_verb
    shipped = Path(__file__).resolve().parent.parent / "etc/sysforge"
    for name in config_schema.CONFIG_FILES:
        shutil.copy(shipped / name, tmp_path / name)
    (tmp_path / "sysforge.toml").write_text("[git]\npull_timeout = 5\n[ui]\ncolr = 1\n[nosuch]\n")
    args = cli_args("config", "check", "--json", "--config-dir", str(tmp_path))
    assert run_verb(ConfigCheckVerb(), args) == 1
    doc = _only_document(capsys.readouterr().out)
    _validate(doc, _schema("config-check"))
    assert [(i["kind"], i["setting"]) for i in doc["issues"]] == [
        ("retired_key", "[git] pull_timeout"), ("unknown_key", "[ui] colr"),
        ("unknown_section", "[nosuch]")]


def test_config_check_json_when_clean(tmp_path, capsys):
    import shutil

    from sysforge.config_cmd import ConfigCheckVerb
    from sysforge.primitives import config_schema
    from sysforge.verbs.runner import run_verb
    shipped = Path(__file__).resolve().parent.parent / "etc/sysforge"
    for name in config_schema.CONFIG_FILES:
        shutil.copy(shipped / name, tmp_path / name)
    args = cli_args("config", "check", "--json", "--config-dir", str(tmp_path))
    assert run_verb(ConfigCheckVerb(), args) == 0
    doc = _only_document(capsys.readouterr().out)
    _validate(doc, _schema("config-check"))
    assert doc["issues"] == []


def test_every_emitted_kind_has_a_schema_and_back():
    """Each `json_out.emit("<kind>", ...)` has tests/data/json-schema/<kind>.schema.json,
    and each schema is for a kind something emits (standards row 32)."""
    import ast
    pkg = Path(__file__).resolve().parent.parent / "sysforge"
    kinds = set()
    for py in pkg.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "emit"
                    and getattr(node.func.value, "id", None) == "json_out"
                    and node.args and isinstance(node.args[0], ast.Constant)):
                kinds.add(node.args[0].value)
    schemas = {p.name.removesuffix(".schema.json") for p in _SCHEMAS.glob("*.schema.json")}
    assert kinds == schemas
