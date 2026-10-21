"""test_gen_options.py - tests for tools/gen_options.py (the sysforge(1) generator).

Behaviour tests build a small synthetic argparse tree so they never depend on
the real CLI's wording; `test_real_parser_*` is the only one that renders the
real tree, as a structural invariant. The rendered-page-matches-committed guard
lives in check_shipped's `manpage` group, not here.
"""
import argparse
import importlib.util
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools/gen_options.py"

_spec = importlib.util.spec_from_file_location("gen_options", SCRIPT)
assert _spec and _spec.loader
gen_options = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_options)


def _tree(build):
    """A root parser whose subcommands ``build(sub)`` registers (with help=,
    or the generator treats them as hidden plumbing)."""
    root = argparse.ArgumentParser(prog="sysforge")
    build(root.add_subparsers(dest="command"))
    return root


def _render(root):
    shared, commands = gen_options.render(root, order=None)
    return "\n".join(shared), "\n".join(commands)


def _section(commands: str, name: str) -> str:
    """The body of one ``## name`` command section."""
    start = commands.index(f"## {name}\n")
    nxt = commands.find("\n## ", start + 1)
    return commands[start:nxt if nxt != -1 else None]


def test_identical_option_renders_once_in_shared_section():
    def build(sub):
        for name in ("alpha", "beta"):
            p = sub.add_parser(name, help=f"{name} command")
            p.add_argument("--state-dir", metavar="DIR", help="Override state directory.")
    shared, commands = _render(_tree(build))
    assert shared.count("*--state-dir* _DIR_") == 1
    assert "Override state directory." in shared
    assert "Used by: *alpha*, *beta*." in shared
    for name in ("alpha", "beta"):
        body = _section(commands, name)
        assert "Override state directory." not in body
        assert "*Shared options:* *--state-dir*" in body


def test_option_in_one_command_stays_inline():
    def build(sub):
        sub.add_parser("alpha", help="alpha command").add_argument(
            "--only-here", action="store_true", help="Alpha-only switch.")
        sub.add_parser("beta", help="beta command")
    shared, commands = _render(_tree(build))
    assert shared == ""
    assert "Alpha-only switch." in _section(commands, "alpha")
    assert "Shared options" not in commands


def test_divergent_help_shares_the_majority_variant_only():
    """One flag, two meanings: the common text is shared, the outlier stays
    inline, and the shared section never lists the flag twice."""
    def build(sub):
        for name in ("alpha", "beta"):
            sub.add_parser(name, help=f"{name} command").add_argument(
                "--dry-run", action="store_true", help="Show what would run.")
        sub.add_parser("gamma", help="gamma command").add_argument(
            "--dry-run", action="store_true", help="Show the planned repair.")
    shared, commands = _render(_tree(build))
    assert shared.count("*--dry-run*") == 1
    assert "Show what would run." in shared
    assert "Show the planned repair." in _section(commands, "gamma")
    assert "Shared options" not in _section(commands, "gamma")


def test_choices_without_metavar_render_as_alternatives():
    def build(sub):
        sub.add_parser("alpha", help="alpha command").add_argument(
            "--mode", choices=("prompt", "auto", "off"), dest="soname_mode",
            help="What to do.")
    _, commands = _render(_tree(build))
    assert "*--mode* _prompt_|_auto_|_off_" in commands
    assert "SONAME_MODE" not in commands


def test_real_parser_shared_section_lists_each_flag_once():
    from sysforge.verbs.registry import build_parser, tiered_command_order

    shared, _ = gen_options.render(build_parser(), order=tiered_command_order())
    heads = [ln for ln in shared if ln.startswith("*--")]
    assert heads, "the real CLI shares no options at all"
    assert len(heads) == len(set(heads))
